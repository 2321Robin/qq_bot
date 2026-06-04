from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.ai_client import AIReplyError, request_ai_reply
from qq_bot.services.chat_memory import ChatMemoryStore
from qq_bot.services.memory_prompt import (
    extract_at_user_ids,
    extract_at_user_ids_before_separator,
    format_chat_context,
    parse_memory_reference,
)
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.onebot_send import finish_with_send_errors_logged
from qq_bot.services.prompt import extract_ai_prompt
from qq_bot.services.roco_knowledge import build_roco_context
from qq_bot.services.search import (
    SearchError,
    format_search_context,
    prompt_needs_search,
    search_web,
)


ai_chat = on_message(priority=20, block=False)


@ai_chat.handle()
async def handle_ai_chat(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    if event.user_id in settings.ai_ignored_user_id_list:
        return

    memory_store: ChatMemoryStore | None = None
    try:
        memory_store = ChatMemoryStore(
            settings.chat_memory_path,
            retention_days=settings.chat_memory_retention_days,
        )
    except Exception:
        logger.exception("Chat memory initialization failed; continuing without memory")

    raw_text = event.get_message().extract_plain_text().strip()
    prompt = extract_ai_prompt(raw_text, prefix=settings.ai_prefix)

    mentioned_self = _mentions_self(event)
    addressed_to_bot = event.is_tome() or mentioned_self
    if prompt is None and addressed_to_bot:
        prompt = raw_text
        if mentioned_self:
            prompt = _strip_leading_self_mention_text(event).strip()

    is_ai_prompt = prompt is not None or addressed_to_bot
    if prompt is None:
        if memory_store is not None:
            try:
                memory_store.add_message(
                    group_id=event.group_id,
                    user_id=event.user_id,
                    message_text=raw_text,
                    is_ai_prompt=is_ai_prompt,
                )
            except Exception:
                logger.exception("Chat memory write failed; continuing without storing message")
        return

    if not prompt:
        await finish_with_send_errors_logged(
            ai_chat,
            f"请在 {settings.ai_prefix} 后面输入要问的问题。",
        )

    if not settings.has_ai_config():
        await finish_with_send_errors_logged(ai_chat, "AI 功能还没有配置 API Key。")

    mentioned_user_ids = _without_self_mentions(
        extract_at_user_ids_before_separator(event.get_message()),
        event,
    )
    memory_reference = parse_memory_reference(
        prompt,
        mentioned_user_ids=mentioned_user_ids,
    )
    prompt = memory_reference.question

    if not prompt:
        await finish_with_send_errors_logged(ai_chat, "请输入要问的问题")

    chat_context = ""
    if memory_store is not None:
        try:
            limit = min(
                memory_reference.limit or settings.chat_memory_default_turns,
                settings.chat_memory_max_results,
            )
            if memory_reference.user_id is not None or memory_reference.keyword:
                rows = memory_store.search_messages(
                    group_id=event.group_id,
                    user_id=memory_reference.user_id,
                    keyword=memory_reference.keyword,
                    limit=limit,
                )
            else:
                rows = memory_store.recent_group_messages(group_id=event.group_id, limit=limit)
            chat_context = format_chat_context(rows)
        except Exception:
            logger.exception("Chat memory read failed; continuing without chat context")

    memory_message_id: int | None = None
    if memory_store is not None:
        try:
            memory_message_id = memory_store.add_message(
                group_id=event.group_id,
                user_id=event.user_id,
                message_text=raw_text,
                is_ai_prompt=is_ai_prompt,
            )
        except Exception:
            logger.exception("Chat memory write failed; continuing without storing message")

    try:
        roco_context = build_roco_context(prompt)
    except Exception:
        logger.exception("Roco knowledge lookup failed; continuing without Roco context")
        roco_context = ""

    search_context = ""
    needs_search = prompt_needs_search(prompt)
    if needs_search and not settings.has_search_config():
        await finish_with_send_errors_logged(
            ai_chat,
            "这个问题需要联网搜索才能可靠回答，但搜索功能还没有配置。",
        )

    if needs_search:
        try:
            search_results = await search_web(prompt, settings=settings)
        except SearchError:
            logger.exception("Web search failed for current-event prompt")
            await finish_with_send_errors_logged(ai_chat, "联网搜索失败了，先不乱编；稍后再问我试试。")
        else:
            if search_results:
                search_context = format_search_context(search_results)
            else:
                await finish_with_send_errors_logged(ai_chat, "联网搜索没有找到可靠结果，先不乱编。")

    try:
        reply = await request_ai_reply(
            prompt,
            settings=settings,
            search_context=search_context,
            chat_context=chat_context,
            roco_context=roco_context,
        )
    except AIReplyError:
        await finish_with_send_errors_logged(ai_chat, "AI 服务暂时不可用，请稍后再试。")

    if memory_message_id is not None:
        try:
            memory_store.update_ai_reply(memory_message_id, reply)
        except Exception:
            logger.exception("Chat memory reply update failed")

    await finish_with_send_errors_logged(ai_chat, replace_named_mentions(reply))


def _mentions_self(event: GroupMessageEvent) -> bool:
    try:
        self_id = int(event.self_id)
    except (TypeError, ValueError):
        return False
    return self_id in extract_at_user_ids(event.get_message())


def _strip_leading_self_mention_text(event: GroupMessageEvent) -> str:
    segments = iter(event.get_message())
    try:
        first_segment = next(segments)
    except StopIteration:
        return event.get_message().extract_plain_text()

    if first_segment.type != "at" or str(first_segment.data.get("qq", "")) != str(event.self_id):
        return event.get_message().extract_plain_text()

    return "".join(
        str(segment.data.get("text", ""))
        for segment in segments
        if segment.type == "text"
    )


def _without_self_mentions(user_ids: list[int], event: GroupMessageEvent) -> list[int]:
    try:
        self_id = int(event.self_id)
    except (TypeError, ValueError):
        return user_ids
    return [user_id for user_id in user_ids if user_id != self_id]
