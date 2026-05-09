from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.ai_client import AIReplyError, request_ai_reply
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

    raw_text = event.get_message().extract_plain_text().strip()
    prompt = extract_ai_prompt(raw_text, prefix=settings.ai_prefix)

    if prompt is None and event.is_tome():
        prompt = raw_text

    if prompt is None:
        return

    if not prompt:
        await ai_chat.finish(f"请在 {settings.ai_prefix} 后面输入要问的问题。")

    if not settings.has_ai_config():
        await ai_chat.finish("AI 功能还没有配置 API Key。")

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
        )
    except AIReplyError:
        await ai_chat.finish("AI 服务暂时不可用，请稍后再试。")

    await ai_chat.finish(replace_named_mentions(reply))
