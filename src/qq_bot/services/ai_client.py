from __future__ import annotations

from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings, get_settings


class AIReplyError(RuntimeError):
    """Raised when the AI provider cannot produce a usable reply."""


class AsyncPostClient(Protocol):
    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> Any:
        raise NotImplementedError


def build_chat_payload(prompt: str, settings: BotSettings) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise AIReplyError("prompt cannot be empty")

    return {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": "你是一个简洁友好的 QQ 群助手。"},
            {"role": "user", "content": cleaned_prompt},
        ],
        "temperature": 0.7,
    }


async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
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
            json=build_chat_payload(prompt, active_settings),
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPError as exc:
        raise AIReplyError("AI API request failed") from exc
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIReplyError("AI API returned an invalid response") from exc
    finally:
        if owns_client and isinstance(active_client, httpx.AsyncClient):
            await active_client.aclose()

    if not content:
        raise AIReplyError("AI API returned an empty response")
    return content
