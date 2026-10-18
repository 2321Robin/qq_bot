"""Admin commands for expression learning and vocabulary management (S10-HUMAN-P1-05).

Three commands, all admin-only (the feature switch never waives authorization —
``spec``: 开关不豁免鉴权):

- ``/表达学习`` triggers one background learning round. The automatic output is
  counts-only: templates/jargon content never reaches logs or metrics, so the
  broadcast carries numbers, not text.
- ``/表达 列表|禁用 <id>`` reads the active template pool (explicit query commands
  display templates by design — the spec's stated exception) or disables one.
- ``/黑话 列表|修正 <词> <释义>|禁用 <词>`` manages the jargon vocabulary.

The learning round runs as a detached task; the handler sends an immediate ack
via ``matcher.send`` (which does not terminate it), and the round itself finishes
the matcher from the inherited context — the same mechanism as the cold-check
delivery in ``ai_chat`` / ``auto_chat``.
"""

from __future__ import annotations

import asyncio

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import get_logger, hash_id
from qq_bot.runtime import RuntimeStateError, get_chat_repository, get_runtime
from qq_bot.services.ai_client import request_completion
from qq_bot.services.expression_learner import RoundSummary, run_learning_round
from qq_bot.services.onebot_send import finish_with_send_errors_logged

logger = get_logger("qq_bot.learn")

_LEARN_PRIORITY = 4  # 先于 /表达（5）匹配，避免 "表达学习" 被前缀歧义吞掉
_MANAGE_PRIORITY = 5
_EXPRESSION_LIST_LIMIT = 10
_JARGON_LIST_LIMIT = 10

learn_command = on_command("表达学习", priority=_LEARN_PRIORITY, block=True)
expr_command = on_command("表达", priority=_MANAGE_PRIORITY, block=True)
jargon_command = on_command("黑话", priority=_MANAGE_PRIORITY, block=True)

_STATUS_MESSAGES = {
    "quota_denied": "今日学习配额已用完",
    "insufficient_material": "最近素材不足，跳过学习",
    "disabled": "表达学习未开启",
    "error": "学习失败，请稍后再试",
}


def _is_admin(event: GroupMessageEvent) -> bool:
    return event.user_id in get_settings().admin_user_id_list


def _reject(component: str) -> None:
    record_error(component, "permission", message="admin command denied: not an admin")


def _broadcast_text(summary: RoundSummary) -> str:
    """Counts-only round report (no template/jargon content ever)."""
    known = _STATUS_MESSAGES.get(summary.status)
    if known is not None:
        return known
    return (
        f"本轮学习完成：扫描 {summary.messages_scanned} 条消息 {summary.batches} 批；"
        f"表达 提取{summary.expressions_extracted} 入库{summary.stored} "
        f"重复{summary.duplicated}；黑话 新词{summary.jargon_new} "
        f"确认{summary.jargon_confirmed}；调用 {summary.llm_calls_used} 次"
    )


async def _run_round(
    matcher,
    *,
    group_id: int,
    memory_store,
    settings,
    quota,
) -> None:
    """One learning round plus its counts-only broadcast.

    Never raises: any failure is reported as the generic ``error`` line so the
    detached task cannot surface into the event loop.
    """
    try:
        summary = await run_learning_round(
            group_id=group_id,
            memory_store=memory_store,
            settings=settings,
            complete=request_completion,
            quota=quota,
        )
    except Exception:
        record_error("learn", "unknown")
        logger.exception("learning round failed: group=%s", hash_id(group_id, kind="group"))
        await finish_with_send_errors_logged(matcher, _STATUS_MESSAGES["error"])
        return
    text = _broadcast_text(summary)
    if summary.status == "ok":
        metrics.COMMANDS.labels("表达学习").inc()
    logger.info(
        "learning round done: group=%s status=%s batches=%d stored=%d jargon_new=%d",
        hash_id(group_id, kind="group"),
        summary.status,
        summary.batches,
        summary.stored,
        summary.jargon_new,
    )
    await finish_with_send_errors_logged(matcher, text)


@learn_command.handle()
async def handle_learn(event: GroupMessageEvent) -> None:
    if not _is_admin(event):
        _reject("learn")
        return
    settings = get_settings()
    if not settings.expression_learn_enabled:
        await finish_with_send_errors_logged(learn_command, _STATUS_MESSAGES["disabled"])
        return
    metrics.COMMANDS.labels("表达学习").inc()
    try:
        memory_store = get_chat_repository()
    except RuntimeStateError:
        logger.exception("learning round requested before runtime is ready")
        await finish_with_send_errors_logged(learn_command, _STATUS_MESSAGES["error"])
        return
    try:
        quota = get_runtime().get_quota_service()
    except RuntimeStateError:
        quota = None
    # 立即回执不终止 handler；完成播报由后台任务承担（matcher 上下文随 task 继承）
    await learn_command.send("开始后台学习…")
    task = asyncio.ensure_future(
        _run_round(
            learn_command,
            group_id=event.group_id,
            memory_store=memory_store,
            settings=settings,
            quota=quota,
        )
    )
    task.add_done_callback(_log_task_failure)


def _log_task_failure(task: asyncio.Future) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - defensive: _run_round never raises
        logger.exception("learning broadcast task failed: %r", exc)


def _split_args(args: Message) -> list[str]:
    return args.extract_plain_text().split()


@expr_command.handle()
async def handle_expression(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    if not _is_admin(event):
        _reject("learn")
        return
    settings = get_settings()
    parts = _split_args(args)
    memory_store = get_chat_repository()
    sub = parts[0] if parts else "列表"
    if sub == "列表":
        rows = await memory_store.list_active_expressions(group_id=event.group_id)
        if not rows:
            await finish_with_send_errors_logged(expr_command, "本群暂无表达模板")
        lines = [f"本群表达模板 {len(rows)} 条（门槛 {settings.expression_min_active}）："]
        for row in rows[:_EXPRESSION_LIST_LIMIT]:
            lines.append(f"#{row.id} {row.template} ×{row.count}")
        await finish_with_send_errors_logged(expr_command, "\n".join(lines))
    if sub == "禁用":
        if len(parts) < 2 or not parts[1].isdigit():
            await finish_with_send_errors_logged(
                expr_command, "用法：/表达 禁用 <id>（编号见 /表达 列表）"
            )
        disabled = await memory_store.disable_expression(expression_id=int(parts[1]))
        if disabled:
            await finish_with_send_errors_logged(expr_command, f"已禁用表达模板 #{parts[1]}。")
        await finish_with_send_errors_logged(expr_command, "未找到或已禁用该表达模板。")
    await finish_with_send_errors_logged(expr_command, "用法：/表达 列表 | /表达 禁用 <id>")


@jargon_command.handle()
async def handle_jargon(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    if not _is_admin(event):
        _reject("learn")
        return
    parts = _split_args(args)
    memory_store = get_chat_repository()
    sub = parts[0] if parts else "列表"
    if sub == "列表":
        rows = await memory_store.list_jargon(
            group_id=event.group_id, status="confirmed", limit=_JARGON_LIST_LIMIT
        )
        unconfirmed = await memory_store.list_jargon(
            group_id=event.group_id, status="unconfirmed", limit=_JARGON_LIST_LIMIT
        )
        if not rows:
            await finish_with_send_errors_logged(
                jargon_command,
                f"暂无已确认黑话（待确认 {len(unconfirmed)} 条）。",
            )
        lines = [f"已确认黑话 {len(rows)} 条（待确认 {len(unconfirmed)} 条）："]
        for row in rows:
            lines.append(f"{row.term}：{row.definition or '（暂无释义）'} ×{row.hits}")
        await finish_with_send_errors_logged(jargon_command, "\n".join(lines))
    if sub == "修正":
        if len(parts) < 3:
            await finish_with_send_errors_logged(jargon_command, "用法：/黑话 修正 <词> <释义>")
        term = parts[1]
        definition = " ".join(parts[2:])
        updated = await memory_store.override_jargon(
            group_id=event.group_id, term=term, definition=definition
        )
        if updated:
            await finish_with_send_errors_logged(jargon_command, f"已修正「{term}」→ {definition}")
        await finish_with_send_errors_logged(jargon_command, f"未找到黑话「{term}」。")
    if sub == "禁用":
        if len(parts) < 2:
            await finish_with_send_errors_logged(jargon_command, "用法：/黑话 禁用 <词>")
        disabled = await memory_store.disable_jargon(group_id=event.group_id, term=parts[1])
        if disabled:
            await finish_with_send_errors_logged(jargon_command, f"已禁用黑话「{parts[1]}」。")
        await finish_with_send_errors_logged(jargon_command, f"未找到黑话「{parts[1]}」。")
    await finish_with_send_errors_logged(
        jargon_command, "用法：/黑话 列表 | /黑话 修正 <词> <释义> | /黑话 禁用 <词>"
    )
