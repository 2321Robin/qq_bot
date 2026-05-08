# QQ Group Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal custom QQ group bot that supports command replies, explicit AI chat, and scheduled group messages.

**Architecture:** The project is a Python package loaded by a `bot.py` NoneBot2 entrypoint. NapCatQQ connects to the NoneBot2 FastAPI driver through OneBot v11 reverse WebSocket, and feature logic lives in small plugin modules backed by testable service modules.

**Tech Stack:** Python 3.11+, NoneBot2, nonebot-adapter-onebot v11, nonebot-plugin-apscheduler, httpx, pydantic-settings, pytest.

---

## File Structure

- Create: `.gitignore` for Python, local env, and local brainstorming artifacts.
- Create: `.env.example` documenting all runtime variables without secret values.
- Create: `pyproject.toml` defining package metadata, runtime dependencies, dev dependencies, pytest settings, and setuptools source layout.
- Create: `bot.py` as the NoneBot2 entrypoint that registers the OneBot v11 adapter and loads local plugins.
- Create: `src/qq_bot/__init__.py` package marker.
- Create: `src/qq_bot/config.py` for typed settings and config parsing helpers.
- Create: `src/qq_bot/services/__init__.py` package marker.
- Create: `src/qq_bot/services/help_text.py` for command help text generation.
- Create: `src/qq_bot/services/prompt.py` for AI prompt trigger parsing.
- Create: `src/qq_bot/services/ai_client.py` for OpenAI-compatible chat completion calls.
- Create: `src/qq_bot/services/scheduled_sender.py` for scheduled batch send helpers.
- Create: `src/qq_bot/plugins/__init__.py` package marker.
- Create: `src/qq_bot/plugins/commands.py` for `/help` and `/ping` handlers.
- Create: `src/qq_bot/plugins/ai_chat.py` for explicit AI chat handling.
- Create: `src/qq_bot/plugins/scheduler.py` for scheduled message registration.
- Create: `tests/test_project_imports.py` for scaffold import verification.
- Create: `tests/test_config.py` for settings parsing.
- Create: `tests/test_help_text.py` for help output.
- Create: `tests/test_prompt.py` for AI trigger parsing.
- Create: `tests/test_ai_client.py` for AI client behavior.
- Create: `tests/test_scheduled_sender.py` for scheduled send behavior.
- Create: `README.md` for local setup, NapCatQQ connection, tests, and manual verification.

## Scope Check

The spec covers one cohesive first version of a QQ group bot. Command replies, AI chat, and scheduled messages share the same runtime and configuration, so they can be implemented in one project plan while keeping each feature isolated in its own service and plugin files.

---

### Task 1: Project Scaffold

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `bot.py`
- Create: `src/qq_bot/__init__.py`
- Create: `src/qq_bot/services/__init__.py`
- Create: `src/qq_bot/plugins/__init__.py`
- Create: `tests/test_project_imports.py`

- [ ] **Step 1: Initialize git for the new project**

Run:

```powershell
git init
```

Expected: a new `.git` directory exists and `git status --short` exits successfully.

- [ ] **Step 2: Create package directories**

Run:

```powershell
New-Item -ItemType Directory -Path "src/qq_bot/services" -Force; New-Item -ItemType Directory -Path "src/qq_bot/plugins" -Force; New-Item -ItemType Directory -Path "tests" -Force
```

Expected: `src/qq_bot/services`, `src/qq_bot/plugins`, and `tests` exist.

- [ ] **Step 3: Create `.gitignore`**

Write `.gitignore`:

```gitignore
.worktrees/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.mypy_cache/
.env
.superpowers/
dist/
build/
*.egg-info/
```

- [ ] **Step 4: Create `.env.example`**

Write `.env.example`:

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8080
COMMAND_START=["/"]
SUPERUSERS=[]

ALLOWED_GROUP_IDS=
ADMIN_USER_IDS=

AI_API_KEY=
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
AI_PREFIX=ai
AI_TIMEOUT_SECONDS=30

SCHEDULED_GROUP_IDS=
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_HOUR=9
SCHEDULED_CRON_MINUTE=0
```

- [ ] **Step 5: Create `pyproject.toml`**

Write `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69.0"]
build-backend = "setuptools.build_meta"

[project]
name = "qq-bot"
version = "0.1.0"
description = "Custom QQ group bot built with NoneBot2 and OneBot v11"
requires-python = ">=3.11"
dependencies = [
  "nonebot2[fastapi]>=2.4.0,<3.0.0",
  "nonebot-adapter-onebot>=2.4.0,<3.0.0",
  "nonebot-plugin-apscheduler>=0.5.0,<1.0.0",
  "httpx>=0.27.0,<1.0.0",
  "pydantic-settings>=2.0.0,<3.0.0",
  "python-dotenv>=1.0.0,<2.0.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0.0,<9.0.0",
  "pytest-asyncio>=0.23.0,<1.0.0",
  "ruff>=0.6.0,<1.0.0",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 6: Create `bot.py`**

Write `bot.py`:

```python
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

plugin_dir = Path(__file__).parent / "src" / "qq_bot" / "plugins"
nonebot.load_plugins(str(plugin_dir))


if __name__ == "__main__":
    nonebot.run()
```

- [ ] **Step 7: Create package markers and smoke test**

Write `src/qq_bot/__init__.py`:

```python
"""Custom QQ group bot package."""
```

Write `src/qq_bot/services/__init__.py`:

```python
"""Pure service helpers for the QQ bot."""
```

Write `src/qq_bot/plugins/__init__.py`:

```python
"""NoneBot plugin modules for the QQ bot."""
```

Write `tests/test_project_imports.py`:

```python
def test_package_imports() -> None:
    import qq_bot

    assert qq_bot.__doc__ == "Custom QQ group bot package."
```

- [ ] **Step 8: Install the project in editable mode**

Run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Expected: pip finishes without dependency resolution errors.

- [ ] **Step 9: Run initial test discovery**

Run:

```powershell
.\.venv\Scripts\python -m pytest
```

Expected: pytest PASS with one scaffold import test.

- [ ] **Step 10: Commit scaffold**

Run:

```powershell
git add .gitignore .env.example pyproject.toml bot.py src/qq_bot/__init__.py src/qq_bot/services/__init__.py src/qq_bot/plugins/__init__.py tests/test_project_imports.py
git commit -m "chore: scaffold qq bot project"
```

Expected: git creates the first project commit.

---

### Task 2: Configuration Module

**Files:**
- Create: `tests/test_config.py`
- Create: `src/qq_bot/config.py`

- [ ] **Step 1: Write failing config tests**

Write `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from qq_bot.config import BotSettings, parse_id_list


def test_parse_id_list_accepts_comma_separated_values() -> None:
    assert parse_id_list("1001, 1002,1003") == [1001, 1002, 1003]


def test_parse_id_list_accepts_empty_value() -> None:
    assert parse_id_list("") == []
    assert parse_id_list(None) == []


def test_settings_expose_group_id_lists() -> None:
    settings = BotSettings(
        allowed_group_ids="1001,1002",
        admin_user_ids="2001",
        scheduled_group_ids="3001, 3002",
    )

    assert settings.allowed_group_id_list == [1001, 1002]
    assert settings.admin_user_id_list == [2001]
    assert settings.scheduled_group_id_list == [3001, 3002]


def test_empty_allowed_group_list_allows_any_group() -> None:
    settings = BotSettings(allowed_group_ids="")

    assert settings.group_allowed(123456)


def test_allowed_group_list_blocks_unknown_group() -> None:
    settings = BotSettings(allowed_group_ids="123456")

    assert settings.group_allowed(123456)
    assert not settings.group_allowed(999999)


def test_scheduled_enabled_requires_group_and_message() -> None:
    disabled = BotSettings(scheduled_group_ids="", scheduled_message="hello")
    enabled = BotSettings(scheduled_group_ids="123456", scheduled_message="hello")

    assert not disabled.scheduled_enabled()
    assert enabled.scheduled_enabled()


def test_invalid_id_list_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="comma-separated integers"):
        BotSettings(allowed_group_ids="123,abc")


def test_invalid_schedule_time_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="scheduled_cron_hour"):
        BotSettings(scheduled_cron_hour=24)

    with pytest.raises(ValidationError, match="scheduled_cron_minute"):
        BotSettings(scheduled_cron_minute=60)
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.config'`.

- [ ] **Step 3: Implement config module**

Write `src/qq_bot/config.py`:

```python
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_id_list(value: str | None) -> list[int]:
    if value is None:
        return []

    text = value.strip()
    if not text:
        return []

    ids: list[int] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError as exc:
            raise ValueError("ID lists must be comma-separated integers") from exc
    return ids


class BotSettings(BaseSettings):
    allowed_group_ids: str = ""
    admin_user_ids: str = ""
    scheduled_group_ids: str = ""
    scheduled_message: str = "现在是定时提醒时间。"
    scheduled_cron_hour: int = 9
    scheduled_cron_minute: int = 0

    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_prefix: str = "ai"
    ai_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("allowed_group_ids", "admin_user_ids", "scheduled_group_ids")
    @classmethod
    def validate_id_list(cls, value: str) -> str:
        parse_id_list(value)
        return value.strip()

    @field_validator("scheduled_cron_hour")
    @classmethod
    def validate_schedule_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("scheduled_cron_hour must be between 0 and 23")
        return value

    @field_validator("scheduled_cron_minute")
    @classmethod
    def validate_schedule_minute(cls, value: int) -> int:
        if value < 0 or value > 59:
            raise ValueError("scheduled_cron_minute must be between 0 and 59")
        return value

    @property
    def allowed_group_id_list(self) -> list[int]:
        return parse_id_list(self.allowed_group_ids)

    @property
    def admin_user_id_list(self) -> list[int]:
        return parse_id_list(self.admin_user_ids)

    @property
    def scheduled_group_id_list(self) -> list[int]:
        return parse_id_list(self.scheduled_group_ids)

    @property
    def normalized_ai_base_url(self) -> str:
        return self.ai_base_url.rstrip("/")

    def group_allowed(self, group_id: int) -> bool:
        allowed_groups = self.allowed_group_id_list
        return not allowed_groups or group_id in allowed_groups

    def has_ai_config(self) -> bool:
        return bool(self.ai_api_key.strip())

    def scheduled_enabled(self) -> bool:
        return bool(self.scheduled_group_id_list) and bool(self.scheduled_message.strip())


@lru_cache(maxsize=1)
def get_settings() -> BotSettings:
    return BotSettings()
```

- [ ] **Step 4: Run config tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

Expected: all tests in `tests/test_config.py` PASS.

- [ ] **Step 5: Commit config module**

Run:

```powershell
git add tests/test_config.py src/qq_bot/config.py
git commit -m "feat: add bot configuration"
```

Expected: git creates a commit for settings parsing.

---

### Task 3: Command Replies

**Files:**
- Create: `tests/test_help_text.py`
- Create: `src/qq_bot/services/help_text.py`
- Create: `src/qq_bot/plugins/commands.py`

- [ ] **Step 1: Write failing help text tests**

Write `tests/test_help_text.py`:

```python
from qq_bot.services.help_text import build_help_text


def test_help_text_lists_supported_commands() -> None:
    text = build_help_text(ai_prefix="ai")

    assert "/help" in text
    assert "/ping" in text
    assert "ai 你好" in text
    assert "定时任务" in text


def test_help_text_uses_configured_ai_prefix() -> None:
    text = build_help_text(ai_prefix="ask")

    assert "ask 你好" in text
```

- [ ] **Step 2: Run help text tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_help_text.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.services.help_text'`.

- [ ] **Step 3: Implement help text service**

Write `src/qq_bot/services/help_text.py`:

```python
def build_help_text(ai_prefix: str) -> str:
    prefix = ai_prefix.strip() or "ai"
    return "\n".join(
        [
            "可用功能：",
            "/help - 查看帮助",
            "/ping - 检查机器人是否在线",
            f"{prefix} 你好 - 向 AI 提问",
            "定时任务 - 由配置文件控制自动发送",
        ]
    )
```

- [ ] **Step 4: Run help text tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_help_text.py -v
```

Expected: all tests in `tests/test_help_text.py` PASS.

- [ ] **Step 5: Implement command plugin**

Write `src/qq_bot/plugins/commands.py`:

```python
from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.help_text import build_help_text


help_command = on_command("help", aliases={"帮助"}, priority=5, block=True)
ping_command = on_command("ping", aliases={"状态"}, priority=5, block=True)


@help_command.handle()
async def handle_help(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await help_command.finish(build_help_text(settings.ai_prefix))


@ping_command.handle()
async def handle_ping(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await ping_command.finish("pong")
```

- [ ] **Step 6: Run tests and import plugin through `bot.py`**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_help_text.py tests/test_config.py -v
.\.venv\Scripts\python -c "import bot; print('bot import ok')"
```

Expected: pytest PASS and the import command prints `bot import ok`.

- [ ] **Step 7: Commit command replies**

Run:

```powershell
git add tests/test_help_text.py src/qq_bot/services/help_text.py src/qq_bot/plugins/commands.py
git commit -m "feat: add command replies"
```

Expected: git creates a commit for `/help` and `/ping`.

---

### Task 4: AI Chat

**Files:**
- Create: `tests/test_prompt.py`
- Create: `tests/test_ai_client.py`
- Create: `src/qq_bot/services/prompt.py`
- Create: `src/qq_bot/services/ai_client.py`
- Create: `src/qq_bot/plugins/ai_chat.py`

- [ ] **Step 1: Write failing prompt parsing tests**

Write `tests/test_prompt.py`:

```python
from qq_bot.services.prompt import extract_ai_prompt


def test_extract_ai_prompt_from_prefix() -> None:
    assert extract_ai_prompt("ai 介绍一下你自己", prefix="ai") == "介绍一下你自己"


def test_extract_ai_prompt_accepts_case_insensitive_prefix() -> None:
    assert extract_ai_prompt("AI 你好", prefix="ai") == "你好"


def test_extract_ai_prompt_returns_empty_string_for_prefix_only() -> None:
    assert extract_ai_prompt("ai", prefix="ai") == ""


def test_extract_ai_prompt_ignores_normal_chat() -> None:
    assert extract_ai_prompt("今天吃什么", prefix="ai") is None


def test_extract_ai_prompt_uses_custom_prefix() -> None:
    assert extract_ai_prompt("ask 天气怎么样", prefix="ask") == "天气怎么样"
```

- [ ] **Step 2: Run prompt tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_prompt.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.services.prompt'`.

- [ ] **Step 3: Implement prompt parsing service**

Write `src/qq_bot/services/prompt.py`:

```python
def extract_ai_prompt(text: str, *, prefix: str) -> str | None:
    marker = prefix.strip()
    if not marker:
        return None

    normalized_text = text.strip()
    normalized_marker = marker.casefold()
    normalized_message = normalized_text.casefold()

    if normalized_message == normalized_marker:
        return ""

    prefix_with_space = f"{normalized_marker} "
    if normalized_message.startswith(prefix_with_space):
        return normalized_text[len(marker) :].strip()

    return None
```

- [ ] **Step 4: Run prompt tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_prompt.py -v
```

Expected: all tests in `tests/test_prompt.py` PASS.

- [ ] **Step 5: Write failing AI client tests**

Write `tests/test_ai_client.py`:

```python
import pytest

from qq_bot.config import BotSettings
from qq_bot.services.ai_client import AIReplyError, build_chat_payload, request_ai_reply


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_build_chat_payload_uses_model_and_prompt() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("你好", settings)

    assert payload["model"] == "test-model"
    assert payload["messages"][-1] == {"role": "user", "content": "你好"}


@pytest.mark.asyncio
async def test_request_ai_reply_posts_openai_compatible_payload() -> None:
    settings = BotSettings(
        ai_api_key="secret",
        ai_base_url="https://api.example.com/v1/",
        ai_model="test-model",
    )
    client = FakeClient(
        FakeResponse({"choices": [{"message": {"content": "机器人回复"}}]})
    )

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "机器人回复"
    assert client.calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert client.calls[0]["json"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_request_ai_reply_requires_api_key() -> None:
    settings = BotSettings(ai_api_key="")
    client = FakeClient(FakeResponse({"choices": []}))

    with pytest.raises(AIReplyError, match="AI_API_KEY"):
        await request_ai_reply("你好", settings=settings, client=client)


@pytest.mark.asyncio
async def test_request_ai_reply_rejects_invalid_response_shape() -> None:
    settings = BotSettings(ai_api_key="secret")
    client = FakeClient(FakeResponse({"choices": []}))

    with pytest.raises(AIReplyError, match="invalid response"):
        await request_ai_reply("你好", settings=settings, client=client)
```

- [ ] **Step 6: Run AI client tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.services.ai_client'`.

- [ ] **Step 7: Implement AI client service**

Write `src/qq_bot/services/ai_client.py`:

```python
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
```

- [ ] **Step 8: Run AI client tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
```

Expected: all tests in `tests/test_ai_client.py` PASS.

- [ ] **Step 9: Implement AI chat plugin**

Write `src/qq_bot/plugins/ai_chat.py`:

```python
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.ai_client import AIReplyError, request_ai_reply
from qq_bot.services.prompt import extract_ai_prompt


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

    try:
        reply = await request_ai_reply(prompt, settings=settings)
    except AIReplyError:
        await ai_chat.finish("AI 服务暂时不可用，请稍后再试。")

    await ai_chat.finish(reply)
```

- [ ] **Step 10: Run AI-related tests and import plugin through `bot.py`**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_prompt.py tests/test_ai_client.py tests/test_config.py -v
.\.venv\Scripts\python -c "import bot; print('bot import ok')"
```

Expected: pytest PASS and the import command prints `bot import ok`.

- [ ] **Step 11: Commit AI chat**

Run:

```powershell
git add tests/test_prompt.py tests/test_ai_client.py src/qq_bot/services/prompt.py src/qq_bot/services/ai_client.py src/qq_bot/plugins/ai_chat.py
git commit -m "feat: add explicit ai chat"
```

Expected: git creates a commit for AI prompt handling and provider calls.

---

### Task 5: Scheduled Messages

**Files:**
- Create: `tests/test_scheduled_sender.py`
- Create: `src/qq_bot/services/scheduled_sender.py`
- Create: `src/qq_bot/plugins/scheduler.py`

- [ ] **Step 1: Write failing scheduled sender tests**

Write `tests/test_scheduled_sender.py`:

```python
import pytest

from qq_bot.config import BotSettings
from qq_bot.services.scheduled_sender import build_scheduler_job_kwargs, send_group_messages


class FakeBot:
    def __init__(self, failing_group_ids: set[int] | None = None):
        self.failing_group_ids = failing_group_ids or set()
        self.sent: list[tuple[int, str]] = []

    async def send_group_msg(self, *, group_id: int, message: str) -> None:
        if group_id in self.failing_group_ids:
            raise RuntimeError("send failed")
        self.sent.append((group_id, message))


def test_build_scheduler_job_kwargs_uses_configured_time() -> None:
    settings = BotSettings(scheduled_cron_hour=8, scheduled_cron_minute=30)

    kwargs = build_scheduler_job_kwargs(settings)

    assert kwargs == {
        "trigger": "cron",
        "hour": 8,
        "minute": 30,
        "id": "daily_group_message",
        "replace_existing": True,
    }


@pytest.mark.asyncio
async def test_send_group_messages_sends_to_each_group() -> None:
    bot = FakeBot()

    failures = await send_group_messages(bot, [1001, 1002], "早上好")

    assert failures == []
    assert bot.sent == [(1001, "早上好"), (1002, "早上好")]


@pytest.mark.asyncio
async def test_send_group_messages_continues_after_failure() -> None:
    bot = FakeBot(failing_group_ids={1002})

    failures = await send_group_messages(bot, [1001, 1002, 1003], "早上好")

    assert failures == [1002]
    assert bot.sent == [(1001, "早上好"), (1003, "早上好")]
```

- [ ] **Step 2: Run scheduled sender tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_scheduled_sender.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.services.scheduled_sender'`.

- [ ] **Step 3: Implement scheduled sender service**

Write `src/qq_bot/services/scheduled_sender.py`:

```python
from __future__ import annotations

from typing import Protocol

from qq_bot.config import BotSettings


class GroupMessageBot(Protocol):
    async def send_group_msg(self, *, group_id: int, message: str) -> object:
        raise NotImplementedError


def build_scheduler_job_kwargs(settings: BotSettings) -> dict[str, object]:
    return {
        "trigger": "cron",
        "hour": settings.scheduled_cron_hour,
        "minute": settings.scheduled_cron_minute,
        "id": "daily_group_message",
        "replace_existing": True,
    }


async def send_group_messages(
    bot: GroupMessageBot,
    group_ids: list[int],
    message: str,
) -> list[int]:
    failed_group_ids: list[int] = []
    for group_id in group_ids:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception:
            failed_group_ids.append(group_id)
    return failed_group_ids
```

- [ ] **Step 4: Run scheduled sender tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_scheduled_sender.py -v
```

Expected: all tests in `tests/test_scheduled_sender.py` PASS.

- [ ] **Step 5: Implement scheduler plugin**

Write `src/qq_bot/plugins/scheduler.py`:

```python
from nonebot import get_bots, logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from qq_bot.config import get_settings
from qq_bot.services.scheduled_sender import build_scheduler_job_kwargs, send_group_messages


require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


async def send_daily_messages() -> None:
    settings = get_settings()
    if not settings.scheduled_enabled():
        logger.info("Scheduled messages are disabled because no target groups are configured.")
        return

    bot = next(
        (connected_bot for connected_bot in get_bots().values() if isinstance(connected_bot, OneBotV11Bot)),
        None,
    )
    if bot is None:
        logger.warning("No OneBot v11 bot is connected; scheduled message skipped.")
        return

    failures = await send_group_messages(
        bot,
        settings.scheduled_group_id_list,
        settings.scheduled_message,
    )
    for group_id in failures:
        logger.warning(f"Scheduled message failed for group {group_id}.")


settings = get_settings()
if settings.scheduled_enabled():
    scheduler.add_job(send_daily_messages, **build_scheduler_job_kwargs(settings))
```

- [ ] **Step 6: Run all automated tests and import plugin through `bot.py`**

Run:

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\python -c "import bot; print('bot import ok')"
```

Expected: pytest PASS and the import command prints `bot import ok`.

- [ ] **Step 7: Commit scheduled messages**

Run:

```powershell
git add tests/test_scheduled_sender.py src/qq_bot/services/scheduled_sender.py src/qq_bot/plugins/scheduler.py
git commit -m "feat: add scheduled group messages"
```

Expected: git creates a commit for scheduled sending.

---

### Task 6: Documentation And Manual Verification

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

Write `README.md`:

````markdown
# QQ Bot

一个基于 NoneBot2、OneBot v11 和 NapCatQQ 的普通 QQ 群自定义机器人。

## 功能

- `/help`：查看机器人帮助。
- `/ping`：检查机器人是否在线。
- `ai 你好`：显式触发 AI 聊天。
- 定时任务：按 `.env` 中的时间向指定群发送消息。

## 本地安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
ALLOWED_GROUP_IDS=你的测试群号
AI_API_KEY=你的模型服务商密钥
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
SCHEDULED_GROUP_IDS=你的测试群号
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_HOUR=9
SCHEDULED_CRON_MINUTE=0
```

## 运行机器人

```powershell
.\.venv\Scripts\python bot.py
```

默认监听地址是 `127.0.0.1:8080`。

## NapCatQQ 连接

在 NapCatQQ 中启用 OneBot v11 反向 WebSocket，连接地址使用：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

如果 NapCatQQ 和机器人不在同一台机器，把 `127.0.0.1` 改成机器人所在机器的局域网或服务器地址。

## 测试

```powershell
.\.venv\Scripts\python -m pytest -v
```

## 手动验证

- 在测试群发送 `/ping`，机器人应回复 `pong`。
- 在测试群发送 `/help`，机器人应回复功能列表。
- 在测试群发送 `ai 你好`，机器人应调用 AI 并回复。
- 把 `SCHEDULED_CRON_HOUR` 和 `SCHEDULED_CRON_MINUTE` 改成接下来几分钟内的时间，机器人应在目标群发送定时消息。
````

- [ ] **Step 2: Run full verification commands**

Run:

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -c "import bot; print('bot import ok')"
```

Expected: pytest PASS, ruff reports no errors, and the import command prints `bot import ok`.

- [ ] **Step 3: Commit documentation**

Run:

```powershell
git add README.md
git commit -m "docs: add qq bot setup guide"
```

Expected: git creates a commit for the setup guide.

---

## Final Verification

- [ ] Run all tests:

```powershell
.\.venv\Scripts\python -m pytest -v
```

Expected: every test passes.

- [ ] Run lint:

```powershell
.\.venv\Scripts\python -m ruff check .
```

Expected: no lint errors.

- [ ] Start the bot:

```powershell
.\.venv\Scripts\python bot.py
```

Expected: NoneBot starts and listens on `127.0.0.1:8080`.

- [ ] Connect NapCatQQ reverse WebSocket to:

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

Expected: NoneBot logs show the OneBot v11 bot connected.

- [ ] In a test QQ group, send `/ping`.

Expected: the bot replies `pong`.

- [ ] In a test QQ group, send `ai 你好`.

Expected: the bot replies with an AI-generated answer when `AI_API_KEY` is configured.

- [ ] Temporarily set the schedule time to the next few minutes and restart the bot.

Expected: the bot sends `SCHEDULED_MESSAGE` to every group in `SCHEDULED_GROUP_IDS`.

## References

- NoneBot manual project entrypoint: https://nonebot.dev/docs/tutorial/application
- NoneBot event handlers and `on_command`: https://nonebot.dev/docs/tutorial/handler
- NoneBot driver selection and FastAPI driver: https://nonebot.dev/docs/next/advanced/driver
- NoneBot scheduler guidance: https://nonebot.dev/docs/next/best-practice/scheduler
- OneBot v11 message segments: https://onebot.adapters.nonebot.dev/docs/api/v11/message/
- OneBot v11 bot send APIs: https://onebot.adapters.nonebot.dev/docs/api/v11/bot/
