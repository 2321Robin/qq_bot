"""Long-term memory commands (S2-MEM-05..08).

All writes/reads are bound to the current event's user_id; group scope is
enforced by ``group_allowed``. Model/tool arguments can never reach these
paths — only explicit user commands can persist memory (S2-TOOL-09).
"""

from __future__ import annotations

import logging

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.runtime import get_chat_repository
from qq_bot.services.layered_memory import LayeredMemoryService
from qq_bot.services.onebot_send import finish_with_send_errors_logged

logger = logging.getLogger("qq_bot.memory_commands")

_PRIORITY = 5  # explicit commands never reach the agent/chat handlers

memory_save_command = on_command("记忆保存", priority=_PRIORITY, block=True)
memory_view_command = on_command("记忆查看", priority=_PRIORITY, block=True)
memory_delete_command = on_command("记忆删除", priority=_PRIORITY, block=True)
memory_close_command = on_command("记忆关闭", priority=_PRIORITY, block=True)


def _service() -> LayeredMemoryService:
    return LayeredMemoryService(get_chat_repository(), get_settings())


@memory_save_command.handle()
async def handle_memory_save(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    content = args.extract_plain_text().strip()
    if not content:
        await finish_with_send_errors_logged(
            memory_save_command,
            "用法：/记忆保存 <内容>（保存当前用户的一条长期偏好）",
        )
    try:
        await _service().save_preference(
            group_id=event.group_id,
            user_id=event.user_id,
            content=content,
        )
    except ValueError as exc:
        await finish_with_send_errors_logged(memory_save_command, f"保存失败：{exc}")
    await finish_with_send_errors_logged(
        memory_save_command,
        f"已保存（仅当前用户可见，最长 {settings.memory_preference_max_chars} 字）。",
    )


@memory_view_command.handle()
async def handle_memory_view(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    service = _service()
    rows, enabled = await service.list_preferences(group_id=event.group_id, user_id=event.user_id)
    state = "已开启" if enabled else "已关闭（长期偏好不会进入对话）"
    if not rows:
        await finish_with_send_errors_logged(
            memory_view_command, f"当前没有已保存的长期偏好（状态：{state}）。"
        )
    lines = [f"长期偏好（状态：{state}）："]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. #{row.id} {row.preference}")
    await finish_with_send_errors_logged(memory_view_command, "\n".join(lines))


@memory_delete_command.handle()
async def handle_memory_delete(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    target = args.extract_plain_text().strip()
    service = _service()
    if target == "全部":
        affected = await service.delete_all(group_id=event.group_id, user_id=event.user_id)
        logger.info(
            "memory delete_all: user=%s group=%s affected_messages=%d",
            event.user_id,
            event.group_id,
            len(affected),
        )
        await finish_with_send_errors_logged(
            memory_delete_command,
            f"已删除当前用户的全部记忆（消息 {len(affected)} 条、AI 回复、偏好与关联摘要）。",
        )
    if not target.isdigit():
        await finish_with_send_errors_logged(
            memory_delete_command, "用法：/记忆删除 <编号|全部>（编号见 /记忆查看）"
        )
    deleted = await service.delete_preference(
        group_id=event.group_id,
        user_id=event.user_id,
        preference_id=int(target),
    )
    if deleted:
        await finish_with_send_errors_logged(memory_delete_command, "已删除该条偏好。")
    await finish_with_send_errors_logged(
        memory_delete_command, "未找到该编号的偏好（只能删除自己的偏好）。"
    )


@memory_close_command.handle()
async def handle_memory_close(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    await _service().close_preferences(group_id=event.group_id, user_id=event.user_id)
    await finish_with_send_errors_logged(
        memory_close_command,
        "已关闭长期偏好并清除全部已保存内容；后续对话不再包含长期记忆层。",
    )
