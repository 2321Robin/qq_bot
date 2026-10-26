"""断线补答插件（S12-CATCHUP-01）：bot 重连后补答掉线期间的呼唤。

NapCat 与后端的反向 WebSocket 每次重建（含 NapCat 容器重启、QQ 静默
掉线后的恢复）都会触发 on_bot_connect；水位推进后重复触发天然幂等。
60 秒防抖吸收 ws 抖动风暴。补答逻辑见 services/catchup.py。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from qq_bot.config import get_settings
from qq_bot.runtime import RuntimeStateError, get_chat_repository, get_runtime
from qq_bot.services.ai_client import request_ai_reply
from qq_bot.services.catchup import CatchupStateStore, run_catch_up

driver = get_driver()

_LOCK = asyncio.Lock()
_LAST_RUN_AT = 0.0
_MIN_INTERVAL_SECONDS = 60.0

# 后台任务保留引用，防止被垃圾回收中途丢弃（与 scheduler 同款）。
_CATCHUP_TASKS: set[asyncio.Task] = set()


@driver.on_bot_connect
async def _on_bot_connect(bot: OneBotV11Bot) -> None:
    if not isinstance(bot, OneBotV11Bot):
        return
    task = asyncio.create_task(_guarded_catch_up(bot))
    _CATCHUP_TASKS.add(task)
    task.add_done_callback(_CATCHUP_TASKS.discard)


async def _guarded_catch_up(bot: OneBotV11Bot) -> None:
    """防抖 + 串行化；真正的补答全程吞异常——补答是尽力而为的增强，
    绝不允许影响刚恢复的连接。"""
    global _LAST_RUN_AT
    async with _LOCK:
        if time.monotonic() - _LAST_RUN_AT < _MIN_INTERVAL_SECONDS:
            logger.debug("catchup debounced: previous round ran recently")
            return
        _LAST_RUN_AT = time.monotonic()
        try:
            await _run(bot)
        except Exception:
            logger.exception("catchup round failed")


async def _run(bot: OneBotV11Bot) -> None:
    settings = get_settings()
    if not settings.catchup_enabled:
        return

    try:
        memory_store = get_chat_repository()
    except Exception:
        logger.warning("chat repository unavailable; catchup runs without memory backfill")
        memory_store = None

    async def quota_check(group_id: int) -> bool:
        try:
            quota_service = get_runtime().get_quota_service()
        except RuntimeStateError:
            return True
        decision = await quota_service.check_admission(scope_type="group", scope_id=group_id)
        return bool(decision.allowed)

    async def group_ids() -> list[int]:
        allowed = settings.allowed_group_id_list
        if allowed:
            return allowed
        raw = await bot.call_api("get_group_list")
        return [int(g.get("group_id")) for g in raw if isinstance(g, dict) and g.get("group_id")]

    async def fetch_history(**kwargs):
        return await bot.call_api("get_group_msg_history", **kwargs)

    async def send(group_id: int, text: str, at_user_id: int | None) -> None:
        message = Message()
        if at_user_id is not None:
            message.append(MessageSegment.at(at_user_id))
            text = f" {text}"
        message.append(MessageSegment.text(text))
        await bot.send_group_msg(group_id=group_id, message=message)

    async def context_rows(group_id: int):
        if memory_store is None:
            return ()
        return await memory_store.recent_group_messages(
            group_id=group_id, limit=settings.chat_memory_default_turns
        )

    await run_catch_up(
        settings=settings,
        state_store=CatchupStateStore(Path("data") / "catchup_state.json"),
        self_id=int(bot.self_id),
        fetch_history_fn=fetch_history,
        group_ids_fn=group_ids,
        send_fn=send,
        reply_fn=request_ai_reply,
        memory_store=memory_store,
        quota_check_fn=quota_check,
        context_rows_fn=context_rows,
    )
