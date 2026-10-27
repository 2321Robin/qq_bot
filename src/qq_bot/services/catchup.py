"""断线补答（S12-CATCHUP-01）。

NapCat 反向 WebSocket 重连（含掉线恢复）后，通过 OneBot 扩展 API
``get_group_msg_history`` 拉取群漫游记录，把掉线窗口内漏掉的消息补回
chat memory，并对其中 @机器人 的呼唤补一条 AI 回答——腾讯静默踢下线
时 NapCat 感知不到断开（2026-07-01 / 2026-08-30 / 2026-10-26 三次同
模式故障），没有这一层，掉线期间的呼唤全部静默丢失。

护栏（防止补答变成惊吓）：
- 首次启动只初始化水位，不回溯历史；
- 水位随在线收发实时推进（S12-CATCHUP-02 LiveWatermark），补答只覆盖
  真掉线窗口，部署重启不再把在线已答过的呼唤重答一遍；
- 只处理水位之后、且在 ``catchup_max_age_seconds`` 窗口内的消息，更久
  的呼唤不补（水位照常推进，一次性跳过）；
- 每群一次最多补 ``catchup_max_replies`` 条呼唤，超出部分只补库不答；
- 补答入库前按「同群同人同文 ±180s」探测去重，在线已处理过的消息
  跳过（水位节流落盘的残余缝隙兜底）。

隐私纪律与全项目一致：日志/指标只记计数与结果，绝不写消息文本。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.observability.logging import get_logger, record_event
from qq_bot.services.memory_prompt import format_chat_context

logger = get_logger("qq_bot.catchup")

# 补答时的额外系统提示：告知模型这是掉线后的补答，语气自然带过即可。
_CATCHUP_HINT = (
    "你刚才掉线了一会儿，刚刚重新上线。这条消息是群友在你掉线期间 @ 你的，"
    "现在补答。回答保持你一贯的简短口语风格；如合适可以顺带提一句刚断线了，"
    "但不必每条都解释。"
)

# 第一条补答前的过渡语（随机挑一条，避免每次掉线都说同一句）。
_ANNOUNCE_VARIANTS: tuple[str, ...] = (
    "（刚才掉线了一会儿，补个答）",
    "（刚断线了，回来看看谁找我）",
    "（掉了会儿线，回来补答一下）",
)

_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]*\]")


@dataclass(frozen=True)
class HistoricalMessage:
    """一条从群漫游记录解析出的历史消息。"""

    time: int  # epoch seconds
    user_id: int
    text: str  # 纯文本（@ 段与 CQ 码剔除）；纯图/表情以占位符表示
    mentions_self: bool


@dataclass(frozen=True)
class CatchupPlan:
    """单个群一次补答的计划（纯函数 plan_catchup 的产物）。"""

    backfill: tuple[HistoricalMessage, ...]  # 按时间升序，需补入库的消息
    calls: tuple[HistoricalMessage, ...]  # 需要补答的呼唤（backfill 的子集）
    first_start: bool  # True = 首次启动，只推进水位不回溯
    skipped_old: int  # 水位之后但超出时间窗、被跳过的条数
    truncated_calls: int  # 超出 max_replies 被裁掉的呼唤数
    new_watermark: int


def parse_history(payload: object, *, self_id: int) -> list[HistoricalMessage]:
    """把 get_group_msg_history 的响应解析为 HistoricalMessage 列表。

    兼容 data.messages / 裸 messages 两种外壳（不同 NapCat 版本字段有出
    入）；消息段兼容 array 与 CQ 字符串两种格式。解析失败的单条跳过，
    机器人自己的消息（user_id == self_id）剔除。
    """
    parsed: list[HistoricalMessage] = []
    for raw in _extract_message_list(payload):
        item = _parse_one(raw, self_id=self_id)
        if item is not None:
            parsed.append(item)
    parsed.sort(key=lambda m: m.time)
    return parsed


def _extract_message_list(payload: object) -> Sequence[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return ()
    data = payload.get("data")
    raw_list: object = None
    if isinstance(payload.get("messages"), list):
        raw_list = payload["messages"]
    elif isinstance(data, Mapping) and isinstance(data.get("messages"), list):
        raw_list = data["messages"]
    elif isinstance(data, list):
        raw_list = data
    if not isinstance(raw_list, list):
        return ()
    return [m for m in raw_list if isinstance(m, Mapping)]


def _parse_one(raw: Mapping[str, Any], *, self_id: int) -> HistoricalMessage | None:
    sender = raw.get("sender")
    if not isinstance(sender, Mapping):
        sender = {}
    try:
        user_id = int(raw.get("user_id") or sender.get("user_id") or 0)
        timestamp = int(raw.get("time") or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or timestamp <= 0 or user_id == self_id:
        return None

    segments = raw.get("message")
    text_parts: list[str] = []
    mentions_self = False
    image_count = 0
    face_count = 0
    if isinstance(segments, str):
        # CQ 码字符串格式：剔除全部 CQ 码（命中自己则记呼唤）。
        mentions_self = f"qq={self_id}]" in segments
        text_parts.append(_CQ_CODE_PATTERN.sub(" ", segments))
    elif isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            seg_type = segment.get("type")
            seg_data = segment.get("data")
            if not isinstance(seg_data, Mapping):
                seg_data = {}
            if seg_type == "text":
                text_parts.append(str(seg_data.get("text") or ""))
            elif seg_type == "at":
                if str(seg_data.get("qq") or "") == str(self_id):
                    mentions_self = True
            elif seg_type == "image":
                image_count += 1
            elif seg_type in ("face", "mface"):
                face_count += 1

    text = " ".join(part.strip() for part in text_parts if part and part.strip())
    if not text:
        # 与 @问答链同款占位（S11-VISION-01）：纯图/表情消息以占位符入记忆。
        if image_count:
            text = "[图片]"
        elif face_count:
            text = "[表情]"
        if not text:
            return None  # 既无文本也无任何可占位内容，跳过
    return HistoricalMessage(
        time=timestamp, user_id=user_id, text=text, mentions_self=mentions_self
    )


def plan_catchup(
    messages: Sequence[HistoricalMessage],
    *,
    watermark: int | None,
    now_epoch: int,
    max_age_seconds: int,
    max_replies: int,
) -> CatchupPlan:
    """纯函数：从历史消息算出本群的补答计划。

    - watermark 为 None 视为首次启动：不回溯，水位直接推进到最新。
    - 候选 = 水位之后的消息；其中早于 ``now_epoch - max_age_seconds`` 的
      整体跳过（skipped_old），窗口内的全部补库、@呼唤另入补答队列。
    - 补答队列超量时保留最新 max_replies 条（truncated_calls 记差额）。
    - 无论跳过多少，水位都推进到本批最大时间——被跳过的消息一次性放行。
    """
    if watermark is None:
        latest = messages[-1].time if messages else now_epoch
        return CatchupPlan(
            backfill=(),
            calls=(),
            first_start=True,
            skipped_old=0,
            truncated_calls=0,
            new_watermark=latest,
        )

    candidates = [m for m in messages if m.time > watermark]
    if not candidates:
        return CatchupPlan(
            backfill=(),
            calls=(),
            first_start=False,
            skipped_old=0,
            truncated_calls=0,
            new_watermark=watermark,
        )

    cutoff = max(watermark, now_epoch - max_age_seconds)
    within = [m for m in candidates if m.time >= cutoff]
    skipped_old = len(candidates) - len(within)
    calls_all = [m for m in within if m.mentions_self and not _is_placeholder_only(m.text)]
    kept_calls = calls_all[-max_replies:] if max_replies > 0 else []
    return CatchupPlan(
        backfill=tuple(within),
        calls=tuple(kept_calls),
        first_start=False,
        skipped_old=skipped_old,
        truncated_calls=len(calls_all) - len(kept_calls),
        new_watermark=candidates[-1].time,
    )


def _is_placeholder_only(text: str) -> bool:
    """只 @ 不说话、或只跟图/表情占位的呼唤不值得调模型补答。"""
    return text.strip() in {"[图片]", "[表情]"}


class CatchupStateStore:
    """水位持久化：data/catchup_state.json，{group_id: epoch 秒}。

    读写失败按空状态处理（最坏情况 = 首次启动语义，不补不答），损坏文件
    会被下次保存覆盖。写入走临时文件 + 原子替换。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[int, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        state: dict[int, int] = {}
        for key, value in raw.items():
            try:
                state[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return state

    def save(self, state: Mapping[int, int]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({str(k): int(v) for k, v in state.items()}),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            logger.warning("catchup state save failed; watermark not persisted")


class LiveWatermark:
    """在线收发推进的补答水位（S12-CATCHUP-02）。

    S12-CATCHUP-01 的水位只有补答轮自己推进——机器人在线时正常收发的每条
    消息完全不碰它，于是"重连窗口"（上次补答水位 → 现在）远大于真正的
    "掉线窗口"：一次部署重启就会把在线期间已回答过的呼唤原样重答一遍
    （2026-10-27 线上实录）。此状态由消息处理器在每条消息入库后 note()，
    补答轮取 ``effective = max(持久化水位, 在线水位)``，重连后补答只覆盖
    真掉线窗口。

    note() 不随消息落盘（热路径零 IO）：内存取每群最大时间戳，节流
    ``flush_interval_seconds`` 合并写回文件；优雅停机由插件钩子补一次
    flush。进程崩溃丢失的最多是一个节流窗口的消息，残余重复由补答入库
    去重兜底。"""

    def __init__(
        self,
        store: CatchupStateStore,
        *,
        flush_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._live: dict[int, int] = {}
        self._flush_interval = flush_interval_seconds
        self._clock = clock
        self._last_flush = clock()

    def note(self, group_id: int, ts: int) -> None:
        """登记一条已处理消息的时间（每群取最大值），必要时节流落盘。"""
        current = self._live.get(group_id)
        if current is None or ts > current:
            self._live[group_id] = int(ts)
        if self._clock() - self._last_flush >= self._flush_interval:
            self._flush_now()

    def get(self, group_id: int) -> int | None:
        return self._live.get(group_id)

    def effective(self, group_id: int, persisted: int | None) -> int | None:
        """补答轮应使用的水位：持久化值与在线值的较大者。"""
        live = self._live.get(group_id)
        if live is None:
            return persisted
        if persisted is None:
            return live
        return max(live, persisted)

    async def flush(self) -> None:
        self._flush_now()

    def _flush_now(self) -> None:
        self._last_flush = self._clock()
        state = self._store.load()
        changed = False
        for group_id, ts in self._live.items():
            if ts > state.get(group_id, 0):
                state[group_id] = ts
                changed = True
        if changed:
            self._store.save(state)


_SHARED_STATE_PATH = Path("data") / "catchup_state.json"
_LIVE_WATERMARK: LiveWatermark | None = None


def shared_live_watermark() -> LiveWatermark:
    """进程级单例：消息处理器 note、补答轮读、停机钩子 flush 共用。"""
    global _LIVE_WATERMARK
    if _LIVE_WATERMARK is None:
        _LIVE_WATERMARK = LiveWatermark(CatchupStateStore(_SHARED_STATE_PATH))
    return _LIVE_WATERMARK


# ---- 编排（所有 IO 以函数注入，服务层不依赖 nonebot 适配器） ----

SendFn = Callable[[int, str, int | None], Awaitable[None]]
"""(group_id, text, at_user_id)。at_user_id 非空时回复需 @ 呼唤者。"""

ReplyFn = Callable[..., Awaitable[str]]
QuotaCheckFn = Callable[[int], Awaitable[bool]]
SleepFn = Callable[[float], Awaitable[None]]


async def run_catch_up(
    *,
    settings: BotSettings,
    state_store: CatchupStateStore,
    self_id: int,
    fetch_history_fn: Callable[..., Awaitable[object]],
    group_ids_fn: Callable[[], Awaitable[list[int]]],
    send_fn: SendFn,
    reply_fn: ReplyFn,
    memory_store: Any | None = None,
    quota_check_fn: QuotaCheckFn | None = None,
    context_rows_fn: Callable[[int], Awaitable[Sequence[Any]]] | None = None,
    sleep_fn: SleepFn = asyncio.sleep,
    now_epoch_fn: Callable[[], int] = lambda: int(time.time()),
    rng: Callable[[], float] = random.random,
    live_watermark: LiveWatermark | None = None,
) -> None:
    """对一个已连接的 bot 跑一轮补答（逐群串行，单群失败不拖累他群）。

    ``live_watermark``（S12-CATCHUP-02）：消息处理器在线推进的水位；None 用
    共享单例。补答按 max(持久化水位, 在线水位) 圈定范围，重连后不再重答
    在线期间已处理过的消息。"""
    if not settings.catchup_enabled:
        return

    try:
        group_ids = await group_ids_fn()
    except Exception:
        metrics.CATCHUP.labels("fetch", "group_list_error").inc()
        logger.warning("catchup group list unavailable; skipping this round")
        return

    state = state_store.load()
    live = live_watermark or shared_live_watermark()
    for group_id in group_ids:
        try:
            await _process_group(
                group_id,
                settings=settings,
                state=state,
                state_store=state_store,
                live=live,
                self_id=self_id,
                fetch_history_fn=fetch_history_fn,
                send_fn=send_fn,
                reply_fn=reply_fn,
                memory_store=memory_store,
                quota_check_fn=quota_check_fn,
                context_rows_fn=context_rows_fn,
                sleep_fn=sleep_fn,
                now_epoch_fn=now_epoch_fn,
                rng=rng,
            )
        except Exception:
            metrics.CATCHUP.labels("group", "error").inc()
            logger.exception("catchup failed for one group; continuing with next")


async def _process_group(
    group_id: int,
    *,
    settings: BotSettings,
    state: dict[int, int],
    state_store: CatchupStateStore,
    live: LiveWatermark,
    self_id: int,
    fetch_history_fn: Callable[..., Awaitable[object]],
    send_fn: SendFn,
    reply_fn: ReplyFn,
    memory_store: Any | None,
    quota_check_fn: QuotaCheckFn | None,
    context_rows_fn: Callable[[int], Awaitable[Sequence[Any]]] | None,
    sleep_fn: SleepFn,
    now_epoch_fn: Callable[[], int],
    rng: Callable[[], float],
) -> None:
    try:
        payload = await fetch_history_fn(group_id=group_id, count=settings.catchup_history_count)
    except Exception as exc:
        metrics.CATCHUP.labels("fetch", "error").inc()
        # 只记异常类型名（如 ActionFailed/TimeoutError），不记消息内容
        logger.warning("catchup history fetch failed for one group: %s", type(exc).__name__)
        return

    messages = parse_history(payload, self_id=self_id)
    plan = plan_catchup(
        messages,
        watermark=live.effective(group_id, state.get(group_id)),
        now_epoch=now_epoch_fn(),
        max_age_seconds=settings.catchup_max_age_seconds,
        max_replies=settings.catchup_max_replies,
    )

    # 水位先落盘再干活：补答中途崩溃也不会下一轮重复补同批消息。
    state[group_id] = plan.new_watermark
    state_store.save(state)
    live.note(group_id, plan.new_watermark)
    if plan.first_start or not plan.backfill:
        # noop 指标区分"跑了无事可做"与"根本没跑"（重连验证依赖它）。
        metrics.CATCHUP.labels("plan", "first_start" if plan.first_start else "noop").inc()
        return

    # 窗口内消息全部补库（记忆完整）；呼唤标记 is_ai_prompt，补答后回填。
    # S12-CATCHUP-02：在线已处理过的消息（handler 已入库同文同行）跳过——
    # 水位缝隙/重启窗口下的重复入库与重复回答双保险；探测失败放行原路径。
    call_rows: dict[HistoricalMessage, int] = {}
    if memory_store is not None:
        call_set = set(plan.calls)
        backfilled = 0
        for m in plan.backfill:
            try:
                duplicate = await memory_store.has_message_like(
                    group_id,
                    m.user_id,
                    m.text,
                    around=datetime.fromtimestamp(m.time, UTC),
                )
            except Exception:
                duplicate = False
            if duplicate:
                metrics.CATCHUP.labels("backfill", "duplicate").inc()
                continue
            try:
                row_id = await memory_store.add_message(
                    group_id=group_id,
                    user_id=m.user_id,
                    message_text=m.text,
                    is_ai_prompt=m in call_set,
                    created_at=datetime.fromtimestamp(m.time, UTC),
                )
            except Exception:
                metrics.CATCHUP.labels("backfill", "error").inc()
                continue
            backfilled += 1
            if m in call_set:
                call_rows[m] = row_id
        if backfilled:
            metrics.CATCHUP.labels("backfill", "ok").inc(backfilled)

    # 全部呼唤都被去重跳过时不再 announce/不耗 quota（无事可做的补答最惊吓）。
    answerable = (
        list(plan.calls) if memory_store is None else [m for m in plan.calls if m in call_rows]
    )
    if not answerable:
        if plan.skipped_old:
            record_event(logger, logging.INFO, "catchup_skipped_old", skipped=plan.skipped_old)
        return

    # quota 准入与 @问答链同权：被拒则整群本轮不补答（也不 announce）。
    if quota_check_fn is not None and not await quota_check_fn(group_id):
        metrics.CATCHUP.labels("reply", "quota_denied").inc()
        return

    if settings.catchup_announce:
        try:
            variant = _ANNOUNCE_VARIANTS[
                min(int(rng() * len(_ANNOUNCE_VARIANTS)), len(_ANNOUNCE_VARIANTS) - 1)
            ]
            await send_fn(group_id, variant, None)
            await sleep_fn(1.0 + rng())
        except Exception:
            metrics.CATCHUP.labels("announce", "error").inc()

    for index, m in enumerate(answerable):
        reply = await _answer_call(
            m,
            settings=settings,
            reply_fn=reply_fn,
            context_rows_fn=context_rows_fn,
            group_id=group_id,
            now_epoch=now_epoch_fn(),
        )
        if reply is None:
            continue
        try:
            await send_fn(group_id, reply, m.user_id)
        except Exception:
            metrics.CATCHUP.labels("reply", "send_error").inc()
            logger.warning("catchup reply send failed for one call")
            continue
        metrics.CATCHUP.labels("reply", "ok").inc()
        row_id = call_rows.get(m)
        if row_id is not None and memory_store is not None:
            try:
                await memory_store.update_ai_reply(row_id, reply)
            except Exception:
                logger.info("catchup reply backfill to memory failed; reply sent anyway")
        if index < len(plan.calls) - 1:
            await sleep_fn(2.0 + 2.0 * rng())

    if plan.truncated_calls:
        record_event(logger, logging.INFO, "catchup_truncated", dropped=plan.truncated_calls)


async def _answer_call(
    m: HistoricalMessage,
    *,
    settings: BotSettings,
    reply_fn: ReplyFn,
    context_rows_fn: Callable[[int], Awaitable[Sequence[Any]]] | None,
    group_id: int,
    now_epoch: int,
) -> str | None:
    chat_context = ""
    if context_rows_fn is not None:
        try:
            rows = await context_rows_fn(group_id)
            chat_context = format_chat_context(list(rows))
        except Exception:
            chat_context = ""
    # 12h 窗口下补答的呼唤可能已经很老：把时效告诉模型，让"在吗"这类
    # 消息得到"来晚了"式的自然回应，而不是当成刚发生的对话。
    age_hint = f"这条消息发出于{_describe_age(now_epoch - m.time)}。"
    try:
        return await reply_fn(
            m.text,
            settings=settings,
            chat_context=chat_context,
            extra_system_prompt=_CATCHUP_HINT + age_hint,
        )
    except Exception:
        metrics.CATCHUP.labels("reply", "model_error").inc()
        logger.warning("catchup model reply failed for one call")
        return None


def _describe_age(seconds: int) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 5400:
        return f"{max(1, seconds // 60)} 分钟前"
    if seconds < 172800:
        return f"{max(1, seconds // 3600)} 小时前"
    return f"{max(1, seconds // 86400)} 天前"
