"""Admin ops commands: quota view and recent quota events (S4-QUOTA-05).

Authorized via ``admin_user_ids``; non-admins are rejected (and the attempt
is recorded). Output is owner-facing: raw integer group/user ids are allowed
here — never in logs or metrics (S4-QUOTA-07). ``detail`` never contains
message bodies.
"""

from __future__ import annotations


from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import get_logger
from qq_bot.runtime import RuntimeStateError, get_runtime
from qq_bot.services.onebot_send import finish_with_send_errors_logged

quota_command = on_command("配额", aliases={"quota"}, priority=5, block=True)
failures_command = on_command("最近故障", priority=5, block=True)

logger = get_logger("qq_bot.ops")


def _is_admin(event: GroupMessageEvent) -> bool:
    return event.user_id in get_settings().admin_user_id_list


def _reject() -> None:
    record_error("ops", "permission", message="admin command denied: not an admin")


@quota_command.handle()
async def handle_quota(event: GroupMessageEvent) -> None:
    if not _is_admin(event):
        _reject()
        return
    metrics.COMMANDS.labels("配额").inc()
    try:
        service = get_runtime().get_quota_service()
    except RuntimeStateError:
        await finish_with_send_errors_logged(quota_command, "运行时未就绪，无法查询配额。")
        return
    if service is None:
        await finish_with_send_errors_logged(quota_command, "配额功能未启用。")
        return
    try:
        group = await service.summary(scope_type="group", scope_id=event.group_id)
        lines = [
            f"群 {group['scope_id']} 今日配额：",
            f"请求 {group['requests']} 次 / 限 {group['rate_limit_per_minute']}/分钟",
            f"Token {group['tokens']} / 已用费用 ${group['cost_usd']:.4f}",
            f"群预算 ${group['group_daily_cost_limit_usd']} / 全局已用上限 ${group['daily_cost_limit_usd']}",
        ]
    except Exception:
        record_error("quota", "unknown")
        logger.exception("quota summary failed")
        await finish_with_send_errors_logged(quota_command, "配额查询失败，请稍后再试。")
        return
    await finish_with_send_errors_logged(quota_command, "\n".join(lines))


@failures_command.handle()
async def handle_failures(event: GroupMessageEvent) -> None:
    if not _is_admin(event):
        _reject()
        return
    metrics.COMMANDS.labels("最近故障").inc()
    try:
        service = get_runtime().get_quota_service()
    except RuntimeStateError:
        await finish_with_send_errors_logged(failures_command, "运行时未就绪，无法查询故障记录。")
        return
    if service is None:
        await finish_with_send_errors_logged(failures_command, "配额功能未启用。")
        return
    try:
        events = await service.recent_failures(limit=10)
    except Exception:
        record_error("quota", "unknown")
        logger.exception("recent failures query failed")
        await finish_with_send_errors_logged(failures_command, "故障记录查询失败，请稍后再试。")
        return
    if not events:
        await finish_with_send_errors_logged(failures_command, "暂无故障记录。")
        return
    lines = ["最近故障："]
    for entry in events:
        lines.append(f"{entry['at'][:19]} 群{entry['scope_id']} {entry['kind']}/{entry['reason']}")
    await finish_with_send_errors_logged(failures_command, "\n".join(lines))
