"""DeepSeek-compatible vision completion client for the calendar sync tool.

Local-tool path only (S9-GAMECAL-SYNC): the serving bot never calls vision, so
this module deliberately stays outside ``ai_client`` — no breaker, no quota,
no fallback chain. One attempt with a precise error message; the CLI user just
re-runs. Images travel as base64 data URLs in OpenAI-style content blocks.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings


class VisionError(RuntimeError):
    """Vision provider is unconfigured, unreachable, or returned unusable output."""


class AsyncJsonPoster(Protocol):
    async def post_json(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]: ...


def image_data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_vision_messages(prompt: str, images: Sequence[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": image_data_url(image)}})
    return [{"role": "user", "content": content}]


def _resolve_api_key(settings: BotSettings) -> str:
    key = settings.ai_vision_api_key.strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise VisionError(
            "vision API key missing: set AI_VISION_API_KEY (or DEEPSEEK_API_KEY) in env/.env"
        )
    return key


def parse_vision_content(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    if not choices:
        raise VisionError(
            f"vision response has no choices: {json.dumps(data, ensure_ascii=False)[:300]}"
        )
    message = (choices[0].get("message") or {}) if isinstance(choices[0], dict) else {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise VisionError("vision response content is empty")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionError(f"vision content is not JSON: {text[:300]}") from exc
    if not isinstance(parsed, dict):
        raise VisionError("vision JSON must be an object")
    return parsed


async def request_vision_json(
    settings: BotSettings,
    prompt: str,
    images: Sequence[Path],
    *,
    poster: AsyncJsonPoster | None = None,
) -> dict[str, Any]:
    """One vision call returning a JSON object; raises VisionError with the reason."""
    if not images:
        raise VisionError("no images supplied to vision call")
    model = settings.ai_vision_model.strip()
    if not model:
        raise VisionError(
            "AI_VISION_MODEL is empty: configure the DeepSeek vision model name first"
        )
    api_key = _resolve_api_key(settings)
    base_url = settings.ai_vision_base_url.strip().rstrip("/")
    payload = {
        "model": model,
        "messages": build_vision_messages(prompt, images),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    if poster is not None:
        data = await poster.post_json(f"{base_url}/chat/completions", json=payload, headers=headers)
    else:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.ai_vision_timeout_seconds)
            ) as client:
                response = await client.post(
                    f"{base_url}/chat/completions", json=payload, headers=headers
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise VisionError(f"vision endpoint unreachable: {exc}") from exc
        except ValueError as exc:
            raise VisionError(f"vision endpoint returned non-JSON body: {exc}") from exc
    return parse_vision_content(data)
