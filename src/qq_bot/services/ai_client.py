from __future__ import annotations

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
) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise AIReplyError("prompt cannot be empty")

    system_prompt = "你是一个简洁友好的 QQ 群助手。请用中文简洁回答。"
    user_content = cleaned_prompt
    cleaned_search_context = search_context.strip()
    if cleaned_search_context:
        system_prompt += " 如果提供了联网搜索资料，请优先依据资料回答；不要编造资料外的来源；适合时附上来源链接。"
        user_content = (
            f"用户问题：{cleaned_prompt}\n\n"
            f"联网搜索资料：\n{cleaned_search_context}"
        )

    return {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
    }


async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    search_context: str = "",
) -> str:
    active_settings = settings or get_settings()
    if not active_settings.has_ai_config():
        raise AIReplyError("AI_API_KEY is not configured")

    owns_client = client is None
    active_client: AsyncPostClient
    if client is None:
        active_client = httpx.AsyncClient(timeout=active_settings.ai_timeout_seconds)
    else:
        active_client = client

    try:
        response = await active_client.post(
            f"{active_settings.normalized_ai_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {active_settings.ai_api_key}",
                "Content-Type": "application/json",
            },
            json=build_chat_payload(
                prompt,
                active_settings,
                search_context=search_context,
            ),
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPError as exc:
        raise AIReplyError("AI API request failed") from exc
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise AIReplyError("AI API returned an invalid response") from exc
    finally:
        if owns_client and isinstance(active_client, httpx.AsyncClient):
            await active_client.aclose()

    if not content:
        raise AIReplyError("AI API returned an empty response")
    return content
