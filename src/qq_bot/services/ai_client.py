from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings, get_settings


class AIReplyError(RuntimeError):
    """Raised when the AI provider cannot produce a usable reply."""


class AsyncPostClient(Protocol):
    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> Any:
        raise NotImplementedError


def build_chat_payload(
    prompt: str,
    settings: BotSettings,
    *,
    search_context: str = "",
    chat_context: str = "",
    roco_context: str = "",
    current_time: str | None = None,
) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise AIReplyError("prompt cannot be empty")

    system_prompt = (
        "你是一个自然的 QQ 群助手，像 QQ 群友聊天。"
        f"当前本地时间：{current_time or _format_current_local_time()}。"
        "先直接回答问题，不要总用“好的”“当然”“我来整理”开头。"
        "语气自然，不要像新闻稿或客服；不确定就说不确定。"
        "不要编造事实，不要编造链接，不要编造时间，不要编造价格。"
        "默认 2-4 句，新闻或搜索类问题可以用 3-5 条短点，控制在 600 字以内。"
    )
    cleaned_search_context = search_context.strip()
    cleaned_chat_context = chat_context.strip()
    cleaned_roco_context = roco_context.strip()
    user_sections = [f"当前用户问题：{cleaned_prompt}"]
    if cleaned_chat_context:
        system_prompt += (
            " 如果提供了历史聊天记录，只把它作为理解前文和用户意图的参考。"
            "不要编造不存在的历史聊天记录；历史不足时要直接说明。"
        )
        user_sections.append(cleaned_chat_context)

    if cleaned_roco_context:
        system_prompt += (
            " 如果提供了本地洛克王国资料，它是可信的本地数据，优先级高于联网搜索和模型记忆；"
            "回答洛克王国精灵、技能、进化问题时优先依据这些本地资料。"
            "不要猜本地资料外的洛克王国数据；资料没有记录或字段为空时，"
            "要直接说本地数据没有记录。"
        )
        user_sections.append(f"本地洛克王国资料：\n{cleaned_roco_context}")

    if cleaned_search_context:
        system_prompt += (
            " 如果提供了联网搜索资料，请优先依据资料回答；"
            "不要编造资料外的信息，不要编造链接，不要编造时间，不要编造价格。"
            "如果搜索资料不足或互相冲突，就说没有可靠来源或信息不一致。"
            "回复末尾加“来源：”，最多 3 条，格式为“1. 标题 - URL”。"
        )
        user_sections.append(f"联网搜索资料：\n{cleaned_search_context}")

    user_content = (
        "\n\n".join(user_sections)
        if cleaned_search_context or cleaned_chat_context or cleaned_roco_context
        else cleaned_prompt
    )

    return {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
        "max_tokens": 600,
    }


async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    search_context: str = "",
    chat_context: str = "",
    roco_context: str = "",
) -> str:
    active_settings = settings or get_settings()
    if not active_settings.has_ai_config():
        raise AIReplyError("AI_API_KEY is not configured")
    if not prompt.strip():
        raise AIReplyError("prompt cannot be empty")

    owns_client = client is None
    active_client: AsyncPostClient
    if client is None:
        active_client = httpx.AsyncClient(timeout=active_settings.ai_timeout_seconds)
    else:
        active_client = client

    try:
        try:
            content = await _request_ai_reply_once(
                prompt,
                settings=active_settings,
                client=active_client,
                base_url=active_settings.normalized_ai_base_url,
                api_key=active_settings.ai_api_key,
                model=active_settings.ai_model,
                search_context=search_context,
                chat_context=chat_context,
                roco_context=roco_context,
            )
        except AIReplyError:
            if not active_settings.has_ai_fallback_config():
                raise
            content = await _request_ai_reply_once(
                prompt,
                settings=active_settings,
                client=active_client,
                base_url=active_settings.normalized_ai_fallback_base_url,
                api_key=active_settings.ai_fallback_api_key,
                model=active_settings.ai_fallback_model,
                search_context=search_context,
                chat_context=chat_context,
                roco_context=roco_context,
            )
    finally:
        if owns_client and isinstance(active_client, httpx.AsyncClient):
            await active_client.aclose()

    return content


async def _request_ai_reply_once(
    prompt: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
    search_context: str,
    chat_context: str,
    roco_context: str,
) -> str:
    payload = build_chat_payload(
        prompt,
        settings.model_copy(update={"ai_model": model}),
        search_context=search_context,
        chat_context=chat_context,
        roco_context=roco_context,
    )

    try:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPError as exc:
        raise AIReplyError("AI API request failed") from exc
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise AIReplyError("AI API returned an invalid response") from exc

    if not content:
        raise AIReplyError("AI API returned an empty response")
    return content


def _format_current_local_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
