"""Vision client tests (S9-GAMECAL-SYNC): payload shape, config gates, response parsing."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from qq_bot.config import BotSettings
from qq_bot.services import vision_client
from qq_bot.services.vision_client import VisionError


def _settings(**overrides) -> BotSettings:
    payload = {"ai_vision_model": "deepseek-v4.1", "ai_vision_api_key": "sk-test"}
    payload.update(overrides)
    return BotSettings(**payload)


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG fake bytes")
    return path


class FakePoster:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def post_json(self, url: str, *, json: dict, headers: dict) -> dict:
        self.calls.append((url, json, headers))
        return self.response


def test_image_data_url_embeds_base64(tmp_path):
    path = _png(tmp_path)
    url = vision_client.image_data_url(path)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG fake bytes"


def test_build_vision_messages_pairs_text_and_images(tmp_path):
    messages = vision_client.build_vision_messages("提取日程", [_png(tmp_path)])
    assert messages[0]["role"] == "user"
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "提取日程"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_missing_model_fails_fast_without_network(tmp_path):
    with pytest.raises(VisionError, match="AI_VISION_MODEL"):
        await vision_client.request_vision_json(
            _settings(ai_vision_model="  "), "p", [_png(tmp_path)]
        )


async def test_missing_api_key_fails_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(VisionError, match="API key"):
        await vision_client.request_vision_json(
            _settings(ai_vision_api_key=""), "p", [_png(tmp_path)]
        )


async def test_request_posts_openai_shape_and_returns_json(tmp_path):
    poster = FakePoster({"choices": [{"message": {"content": json.dumps({"events": []})}}]})
    data = await vision_client.request_vision_json(
        _settings(ai_vision_base_url="https://api.deepseek.com/v1"),
        "提取日程",
        [_png(tmp_path)],
        poster=poster,
    )
    assert data == {"events": []}
    url, payload, headers = poster.calls[0]
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert payload["model"] == "deepseek-v4.1"
    assert payload["response_format"] == {"type": "json_object"}
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["messages"][0]["content"][0]["type"] == "text"


async def test_strips_code_fences_from_content(tmp_path):
    fenced = '```json\n{"events": [{"ok": true}]}\n```'
    poster = FakePoster({"choices": [{"message": {"content": fenced}}]})
    data = await vision_client.request_vision_json(
        _settings(), "p", [_png(tmp_path)], poster=poster
    )
    assert data == {"events": [{"ok": True}]}


async def test_rejects_non_json_and_empty_content(tmp_path):
    bad_json = FakePoster({"choices": [{"message": {"content": "not json"}}]})
    with pytest.raises(VisionError, match="not JSON"):
        await vision_client.request_vision_json(_settings(), "p", [_png(tmp_path)], poster=bad_json)
    empty = FakePoster({"choices": [{"message": {"content": "  "}}]})
    with pytest.raises(VisionError, match="empty"):
        await vision_client.request_vision_json(_settings(), "p", [_png(tmp_path)], poster=empty)
    no_choices = FakePoster({"choices": []})
    with pytest.raises(VisionError, match="no choices"):
        await vision_client.request_vision_json(
            _settings(), "p", [_png(tmp_path)], poster=no_choices
        )


async def test_no_images_fails_fast():
    with pytest.raises(VisionError, match="no images"):
        await vision_client.request_vision_json(_settings(), "p", [])
