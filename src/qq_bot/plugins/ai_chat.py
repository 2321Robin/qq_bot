from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.ai_client import AIReplyError, request_ai_reply
from qq_bot.services.chat_memory import ChatMemoryStore
from qq_bot.services.memory_prompt import (
    extract_at_user_ids,
    format_chat_context,
    parse_memory_reference,
)
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.prompt import extract_ai_prompt
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

    if prompt is None and event.is_tome():
        prompt = raw_text

    is_ai_prompt = prompt is not None or event.is_tome()
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
        await ai_chat.finish(f"请在 {settings.ai_prefix} 后面输入要问的问题。")

    if not settings.has_ai_config():
        await ai_chat.finish("AI 功能还没有配置 API Key。")

    mentioned_user_ids = extract_at_user_ids(event.get_message())
    memory_reference = parse_memory_reference(
        prompt,
        mentioned_user_ids=mentioned_user_ids,
    )
    prompt = memory_reference.question

    if not prompt:
        await ai_chat.finish(f"请在 {settings.ai_prefix} 后面输入要问的问题。")

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
            elif memory_reference.limit is not None:
                rows = memory_store.recent_group_messages(group_id=event.group_id, limit=limit)
            else:
                rows = memory_store.recent_user_turns(
                    group_id=event.group_id,
                    user_id=event.user_id,
                    limit=limit,
                )
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

    search_context = ""
    if prompt_needs_search(prompt) and settings.has_search_config():
        try:
            search_results = await search_web(prompt, settings=settings)
        except SearchError:
            logger.exception("Web search failed; falling back to direct AI reply")
        else:
            if search_results:
                search_context = format_search_context(search_results)

    try:
        reply = await request_ai_reply(
            prompt,
            settings=settings,
            search_context=search_context,
            chat_context=chat_context,
        )
    except AIReplyError:
        await ai_chat.finish("AI 服务暂时不可用，请稍后再试。")

    if memory_message_id is not None:
        try:
            memory_store.update_ai_reply(memory_message_id, reply)
        except Exception:
            logger.exception("Chat memory reply update failed")

    await ai_chat.finish(replace_named_mentions(reply))
