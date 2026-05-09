# AI Web Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional Tavily-powered web search to AI chat replies when prompts ask for current or searchable information.

**Architecture:** Keep the existing QQ/NapCat/NoneBot AI entrypoint. Add search configuration to `BotSettings`, a focused `search.py` service for trigger detection, Tavily calls, and context formatting, then pass optional search context into the existing DeepSeek chat-completions payload.

**Tech Stack:** Python 3.11, NoneBot2, OneBot v11 adapter, `httpx`, Pydantic Settings, Tavily Search API, pytest, Ruff.

---

## Reference Behavior

- Tavily Search API endpoint: `POST https://api.tavily.com/search`
- Tavily auth: `Authorization: Bearer <TAVILY_API_KEY>`
- Tavily request fields used in v1: `query`, `search_depth`, `max_results`, `include_answer`, `include_raw_content`
- Tavily response fields used in v1: `results[].title`, `results[].url`, `results[].content`
- DeepSeek Chat Completion does not expose an official built-in `web_search` parameter in the reviewed docs; it supports function tools, but v1 uses our own deterministic search-before-answer flow instead of model tool-calling.

Official docs checked during planning:

- `https://docs.tavily.com/documentation/api-reference/endpoint/search`
- `https://docs.tavily.com/documentation/api-credits`
- `https://api-docs.deepseek.com/api/create-chat-completion`

## File Structure

- Modify `src/qq_bot/config.py`: add search settings, validators, and `has_search_config()`.
- Create `src/qq_bot/services/search.py`: trigger detection, Tavily request, response normalization, context formatting.
- Modify `src/qq_bot/services/ai_client.py`: let AI payload include optional search context.
- Modify `src/qq_bot/plugins/ai_chat.py`: run search only for trigger prompts and degrade safely.
- Modify `.env.example`: document search variables without secrets.
- Modify `README.md`: explain Tavily free key setup and smart search behavior.
- Modify tests: `tests/test_config.py`, `tests/test_ai_client.py`, `tests/test_ai_chat_plugin.py`.
- Create tests: `tests/test_search.py`.

---

### Task 1: Search Configuration

**Files:**
- Modify: `src/qq_bot/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Append these tests to `tests/test_config.py`:

```python
def test_search_settings_are_exposed_and_secret_is_hidden() -> None:
    settings = BotSettings(
        search_enabled=True,
        tavily_api_key="tvly-secret",
        search_max_results=3,
        search_timeout_seconds=7,
    )

    assert settings.search_enabled is True
    assert settings.tavily_api_key == "tvly-secret"
    assert settings.search_max_results == 3
    assert settings.search_timeout_seconds == 7
    assert "tvly-secret" not in repr(settings)


def test_has_search_config_requires_enabled_and_key() -> None:
    assert BotSettings(search_enabled=True, tavily_api_key="tvly-secret").has_search_config()
    assert not BotSettings(search_enabled=False, tavily_api_key="tvly-secret").has_search_config()
    assert not BotSettings(search_enabled=True, tavily_api_key="   ").has_search_config()


def test_invalid_search_limits_raise_validation_error() -> None:
    with pytest.raises(ValidationError, match="search_max_results"):
        BotSettings(search_max_results=0)

    with pytest.raises(ValidationError, match="search_timeout_seconds"):
        BotSettings(search_timeout_seconds=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

Expected: FAIL because `BotSettings` has no `search_enabled`, `tavily_api_key`, `search_max_results`, `search_timeout_seconds`, or `has_search_config()` yet.

- [ ] **Step 3: Implement search settings**

Update `src/qq_bot/config.py` by adding the new fields after the AI fields:

```python
    search_enabled: bool = False
    tavily_api_key: str = Field(default="", repr=False)
    search_max_results: int = 5
    search_timeout_seconds: float = 10.0
```

Add validators after `validate_schedule_minute()`:

```python
    @field_validator("search_max_results")
    @classmethod
    def validate_search_max_results(cls, value: int) -> int:
        if value < 1 or value > 20:
            raise ValueError("search_max_results must be between 1 and 20")
        return value

    @field_validator("search_timeout_seconds")
    @classmethod
    def validate_search_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("search_timeout_seconds must be greater than 0")
        return value
```

Add this method after `has_ai_config()`:

```python
    def has_search_config(self) -> bool:
        return self.search_enabled and bool(self.tavily_api_key.strip())
```

- [ ] **Step 4: Run config tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 5: Run lint on touched files**

Run:

```powershell
.\.venv\Scripts\python -m ruff check src/qq_bot/config.py tests/test_config.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit Task 1**

Run:

```powershell
git add src/qq_bot/config.py tests/test_config.py
git commit -m "feat: add web search settings"
```

---

### Task 2: Tavily Search Service

**Files:**
- Create: `src/qq_bot/services/search.py`
- Create: `tests/test_search.py`

- [ ] **Step 1: Write failing search service tests**

Create `tests/test_search.py`:

```python
import pytest

from qq_bot.config import BotSettings
from qq_bot.services.search import (
    SearchError,
    SearchResult,
    format_search_context,
    prompt_needs_search,
    search_web,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class InvalidJsonResponse(FakeResponse):
    def json(self) -> dict:
        raise ValueError("not json")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url: str, *, headers: dict[str, str], json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_prompt_needs_search_detects_current_information_requests() -> None:
    assert prompt_needs_search("今天有什么新闻")
    assert prompt_needs_search("帮我联网查一下 DeepSeek 最新消息")
    assert prompt_needs_search("OpenAI 官网是什么")
    assert not prompt_needs_search("讲个笑话")


@pytest.mark.asyncio
async def test_search_web_posts_tavily_payload_and_normalizes_results() -> None:
    settings = BotSettings(
        search_enabled=True,
        tavily_api_key="tvly-secret",
        search_max_results=2,
    )
    client = FakeClient(
        FakeResponse(
            {
                "results": [
                    {
                        "title": "Result One",
                        "url": "https://example.com/one",
                        "content": "First summary",
                    },
                    {
                        "title": "Result Two",
                        "url": "https://example.com/two",
                        "content": "Second summary",
                    },
                ]
            }
        )
    )

    results = await search_web("DeepSeek 最新消息", settings=settings, client=client)

    assert results == [
        SearchResult(
            title="Result One",
            url="https://example.com/one",
            content="First summary",
        ),
        SearchResult(
            title="Result Two",
            url="https://example.com/two",
            content="Second summary",
        ),
    ]
    assert client.calls[0]["url"] == "https://api.tavily.com/search"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer tvly-secret"
    assert client.calls[0]["json"] == {
        "query": "DeepSeek 最新消息",
        "search_depth": "basic",
        "max_results": 2,
        "include_answer": False,
        "include_raw_content": False,
    }


@pytest.mark.asyncio
async def test_search_web_requires_search_config() -> None:
    settings = BotSettings(search_enabled=True, tavily_api_key="")
    client = FakeClient(FakeResponse({"results": []}))

    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        await search_web("query", settings=settings, client=client)


@pytest.mark.asyncio
async def test_search_web_rejects_invalid_response() -> None:
    settings = BotSettings(search_enabled=True, tavily_api_key="tvly-secret")
    client = FakeClient(InvalidJsonResponse({}))

    with pytest.raises(SearchError, match="invalid response"):
        await search_web("query", settings=settings, client=client)


def test_format_search_context_includes_numbered_sources() -> None:
    context = format_search_context(
        [
            SearchResult("Title One", "https://example.com/one", "First summary"),
            SearchResult("Title Two", "https://example.com/two", "Second summary"),
        ]
    )

    assert "[1] Title One" in context
    assert "https://example.com/one" in context
    assert "First summary" in context
    assert "[2] Title Two" in context
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_search.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'qq_bot.services.search'`.

- [ ] **Step 3: Implement `search.py`**

Create `src/qq_bot/services/search.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_TRIGGERS = (
    "搜索",
    "联网",
    "查一下",
    "查下",
    "最新",
    "今天",
    "现在",
    "新闻",
    "价格",
    "天气",
    "官网",
    "search",
    "latest",
    "today",
    "news",
)


class SearchError(RuntimeError):
    """Raised when web search cannot produce usable results."""


class AsyncPostClient(Protocol):
    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: str


def prompt_needs_search(prompt: str) -> bool:
    text = prompt.casefold()
    return any(trigger.casefold() in text for trigger in SEARCH_TRIGGERS)


def format_search_context(results: list[SearchResult]) -> str:
    lines: list[str] = []
    for index, result in enumerate(results, start=1):
        lines.append(f"[{index}] {result.title}\nURL: {result.url}\n摘要: {result.content}")
    return "\n\n".join(lines)


async def search_web(
    query: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient | None = None,
) -> list[SearchResult]:
    if not settings.has_search_config():
        raise SearchError("TAVILY_API_KEY is not configured")

    owns_client = client is None
    active_client: AsyncPostClient
    if client is None:
        active_client = httpx.AsyncClient(timeout=settings.search_timeout_seconds)
    else:
        active_client = client

    try:
        response = await active_client.post(
            TAVILY_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {settings.tavily_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query.strip(),
                "search_depth": "basic",
                "max_results": settings.search_max_results,
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        raw_results = data["results"]
    except httpx.HTTPError as exc:
        raise SearchError("Tavily search request failed") from exc
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SearchError("Tavily search returned an invalid response") from exc
    finally:
        if owns_client and isinstance(active_client, httpx.AsyncClient):
            await active_client.aclose()

    results: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        content = str(item.get("content", "")).strip()
        if title and url and content:
            results.append(SearchResult(title=title, url=url, content=content))
    return results
```

- [ ] **Step 4: Run search tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_search.py -v
```

Expected: PASS.

- [ ] **Step 5: Run lint on search files**

Run:

```powershell
.\.venv\Scripts\python -m ruff check src/qq_bot/services/search.py tests/test_search.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit Task 2**

Run:

```powershell
git add src/qq_bot/services/search.py tests/test_search.py
git commit -m "feat: add Tavily search service"
```

---

### Task 3: AI Payload Search Context

**Files:**
- Modify: `src/qq_bot/services/ai_client.py`
- Modify: `tests/test_ai_client.py`

- [ ] **Step 1: Write failing AI payload tests**

Update `tests/test_ai_client.py` so imports still include `AIReplyError`, `build_chat_payload`, and `request_ai_reply`. Add these tests:

```python
def test_build_chat_payload_includes_search_context_when_provided() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "今天新闻",
        settings,
        search_context="[1] Example\nURL: https://example.com\n摘要: summary",
    )

    assert payload["model"] == "test-model"
    assert "联网搜索资料" in payload["messages"][-1]["content"]
    assert "https://example.com" in payload["messages"][-1]["content"]
    assert "优先依据资料" in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_request_ai_reply_posts_search_context_payload() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "带来源回复"}}]}))

    reply = await request_ai_reply(
        "今天新闻",
        settings=settings,
        client=client,
        search_context="[1] Example\nURL: https://example.com\n摘要: summary",
    )

    assert reply == "带来源回复"
    user_message = client.calls[0]["json"]["messages"][-1]["content"]
    assert "今天新闻" in user_message
    assert "联网搜索资料" in user_message
    assert "https://example.com" in user_message
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
```

Expected: FAIL because `build_chat_payload()` and `request_ai_reply()` do not accept `search_context` yet.

- [ ] **Step 3: Implement optional search context**

Update the `build_chat_payload` signature in `src/qq_bot/services/ai_client.py`:

```python
def build_chat_payload(
    prompt: str,
    settings: BotSettings,
    *,
    search_context: str = "",
) -> dict[str, Any]:
```

Replace its return construction with:

```python
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
```

Update `request_ai_reply` signature:

```python
async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    search_context: str = "",
) -> str:
```

Update the POST JSON argument:

```python
            json=build_chat_payload(
                prompt,
                active_settings,
                search_context=search_context,
            ),
```

- [ ] **Step 4: Run AI client tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
```

Expected: PASS.

- [ ] **Step 5: Run lint on AI client files**

Run:

```powershell
.\.venv\Scripts\python -m ruff check src/qq_bot/services/ai_client.py tests/test_ai_client.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
git add src/qq_bot/services/ai_client.py tests/test_ai_client.py
git commit -m "feat: add search context to AI replies"
```

---

### Task 4: AI Plugin Search Flow

**Files:**
- Modify: `src/qq_bot/plugins/ai_chat.py`
- Modify: `tests/test_ai_chat_plugin.py`

- [ ] **Step 1: Update existing AI plugin test helper**

In `tests/test_ai_chat_plugin.py`, change the existing `fake_request_ai_reply` in `test_ai_chat_formats_named_mentions_in_final_reply` to accept `search_context`:

```python
    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
    ) -> str:
        assert prompt == "提醒我"
        assert settings.ai_api_key == "secret"
        assert search_context == ""
        return "好的，@小呱呱 会收到提醒"
```

- [ ] **Step 2: Write failing plugin search tests**

Append these tests to `tests/test_ai_chat_plugin.py`:

```python
@pytest.mark.asyncio
async def test_ai_chat_uses_search_context_for_search_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.search import SearchResult

    async def fake_search_web(prompt: str, *, settings: BotSettings):
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert settings.tavily_api_key == "tvly-secret"
        return [SearchResult("DeepSeek News", "https://example.com/news", "news summary")]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
    ) -> str:
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert "DeepSeek News" in search_context
        assert "https://example.com/news" in search_context
        return "根据搜索结果，DeepSeek 有新闻。"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天 DeepSeek 有什么新闻"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_skips_search_for_normal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search_web(prompt: str, *, settings: BotSettings):
        raise AssertionError("search should not be called")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
    ) -> str:
        assert prompt == "讲个笑话"
        assert search_context == ""
        return "一个简短笑话。"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 讲个笑话"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_falls_back_when_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.search import SearchError

    async def fake_search_web(prompt: str, *, settings: BotSettings):
        raise SearchError("search down")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
    ) -> str:
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert search_context == ""
        return "没有联网资料时的普通回复。"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天 DeepSeek 有什么新闻"))  # type: ignore[arg-type]
```

- [ ] **Step 3: Run plugin tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_chat_plugin.py -v
```

Expected: FAIL because `ai_chat_plugin` does not import or call `search_web`, and `request_ai_reply` calls do not pass `search_context` yet.

- [ ] **Step 4: Implement plugin search flow**

Update `src/qq_bot/plugins/ai_chat.py` imports:

```python
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
```

Replace the AI request block with:

```python
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
```

Keep the final send unchanged:

```python
    await ai_chat.finish(replace_named_mentions(reply))
```

- [ ] **Step 5: Run plugin tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_chat_plugin.py -v
```

Expected: PASS.

- [ ] **Step 6: Run lint on plugin files**

Run:

```powershell
.\.venv\Scripts\python -m ruff check src/qq_bot/plugins/ai_chat.py tests/test_ai_chat_plugin.py
```

Expected: `All checks passed!`

- [ ] **Step 7: Commit Task 4**

Run:

```powershell
git add src/qq_bot/plugins/ai_chat.py tests/test_ai_chat_plugin.py
git commit -m "feat: enable smart AI web search"
```

---

### Task 5: Environment And README Documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Update `.env.example`**

Add these lines after `AI_TIMEOUT_SECONDS=30`:

```dotenv
SEARCH_ENABLED=false
TAVILY_API_KEY=
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SECONDS=10
```

- [ ] **Step 2: Update README configuration example**

In `README.md`, add the same search variables in the configuration example after `AI_TIMEOUT_SECONDS=30`:

```dotenv
SEARCH_ENABLED=false
TAVILY_API_KEY=
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SECONDS=10
```

Add this paragraph below the existing configuration notes:

```markdown
`SEARCH_ENABLED=true` enables smart web search for prompts that look like they need current information, such as `ai 今天有什么新闻` or `ai 搜索 DeepSeek 最新消息`. Create a free Tavily API key at `https://app.tavily.com/`, set `TAVILY_API_KEY`, then restart the bot. Tavily's free plan provides monthly free API credits; keep the key only in local `.env`.
```

Add this manual verification step in the verification list:

```markdown
- Set `SEARCH_ENABLED=true` and `TAVILY_API_KEY`, restart the bot, send `ai 搜索 DeepSeek 最新消息`, and expect an answer that uses web-search context.
```

- [ ] **Step 3: Run docs-related tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_startup_scripts.py -v
.\.venv\Scripts\python -m ruff check .
```

Expected: startup script tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 4: Commit Task 5**

Run:

```powershell
git add .env.example README.md
git commit -m "docs: document AI web search setup"
```

---

### Task 6: Final Verification

**Files:**
- No file changes expected unless verification reveals a defect.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py tests/test_search.py tests/test_ai_client.py tests/test_ai_chat_plugin.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```powershell
.\.venv\Scripts\python -m pytest -v
```

Expected: PASS with zero failures.

- [ ] **Step 3: Run full lint**

Run:

```powershell
.\.venv\Scripts\python -m ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 4: Verify bot import**

Run:

```powershell
.\.venv\Scripts\python -c "import bot; print('bot import ok')"
```

Expected: command exits successfully and prints `bot import ok` after plugin load logs.

- [ ] **Step 5: Check git status**

Run:

```powershell
git status --short --branch
```

Expected: clean feature branch after all task commits.

---

## Plan Self-Review

- Spec coverage: configuration, Tavily search service, smart trigger behavior, AI context injection, fallback handling, docs, and final verification are covered.
- Placeholder scan: no placeholder tasks or unspecified code blocks remain.
- Type consistency: `search_context` is a keyword-only string passed from plugin to `request_ai_reply()` to `build_chat_payload()`. `SearchResult` is used consistently by search service and plugin tests. `has_search_config()` belongs to `BotSettings`.
- Scope check: v1 remains a single-provider search-before-answer feature; tool-calling, multi-provider search, scraping, and memory remain out of scope.
