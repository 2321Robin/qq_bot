# AI Chat Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent 3-day group chat memory so AI replies can use current group-user context by default and explicitly selected history on demand.

**Architecture:** Keep `qq_bot.plugins.ai_chat` as the single AI entrypoint. Add a focused SQLite-backed `chat_memory` service for persistence and querying, a small `memory_prompt` parser/formatter for explicit reference syntax, then pass optional `chat_context` into the existing OpenAI-compatible chat payload.

**Tech Stack:** Python 3.11, standard-library `sqlite3`, NoneBot2, OneBot v11 adapter, Pydantic Settings, pytest, pytest-asyncio, Ruff.

---

## File Structure

- Create `src/qq_bot/services/chat_memory.py`: owns SQLite schema creation, message writes, reply updates, 3-day cleanup, and query helpers.
- Create `src/qq_bot/services/memory_prompt.py`: parses `参考...` history-reference syntax and formats memory rows for the AI prompt.
- Modify `src/qq_bot/config.py`: add memory path and retention/context limits.
- Modify `src/qq_bot/services/ai_client.py`: accept `chat_context` and include it in the payload.
- Modify `src/qq_bot/plugins/ai_chat.py`: record allowed group messages, resolve default/explicit context, and degrade safely on memory errors.
- Modify `.env.example`: document memory settings.
- Modify `README.md`: document memory behavior and example prompts.
- Create `tests/test_chat_memory.py`: service-level SQLite coverage.
- Create `tests/test_memory_prompt.py`: parser and formatter coverage.
- Modify `tests/test_config.py`, `tests/test_ai_client.py`, and `tests/test_ai_chat_plugin.py`.

---

### Task 1: Memory Configuration

**Files:**
- Modify: `src/qq_bot/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Append to `tests/test_config.py`:

```python
from pydantic import ValidationError


def test_chat_memory_settings_are_exposed() -> None:
    settings = BotSettings(
        chat_memory_path="data/test-memory.sqlite3",
        chat_memory_retention_days=3,
        chat_memory_default_turns=10,
        chat_memory_max_results=20,
    )

    assert settings.chat_memory_path == "data/test-memory.sqlite3"
    assert settings.chat_memory_retention_days == 3
    assert settings.chat_memory_default_turns == 10
    assert settings.chat_memory_max_results == 20


def test_chat_memory_settings_validate_positive_limits() -> None:
    with pytest.raises(ValidationError, match="chat_memory_retention_days"):
        BotSettings(chat_memory_retention_days=0)

    with pytest.raises(ValidationError, match="chat_memory_default_turns"):
        BotSettings(chat_memory_default_turns=0)

    with pytest.raises(ValidationError, match="chat_memory_max_results"):
        BotSettings(chat_memory_max_results=0)
```

If `tests/test_config.py` already imports `ValidationError`, do not add a duplicate import. If it does not import `pytest`, add `import pytest` at the top.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

Expected: FAIL because `BotSettings` does not expose the chat memory settings.

- [ ] **Step 3: Implement settings**

In `src/qq_bot/config.py`, add fields after the AI settings:

```python
    chat_memory_path: str = "data/chat_memory.sqlite3"
    chat_memory_retention_days: int = 3
    chat_memory_default_turns: int = 10
    chat_memory_max_results: int = 20
```

Add validators after the existing timeout validators:

```python
    @field_validator("chat_memory_retention_days")
    @classmethod
    def validate_chat_memory_retention_days(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_retention_days must be greater than 0")
        return value

    @field_validator("chat_memory_default_turns")
    @classmethod
    def validate_chat_memory_default_turns(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_default_turns must be greater than 0")
        return value

    @field_validator("chat_memory_max_results")
    @classmethod
    def validate_chat_memory_max_results(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_max_results must be greater than 0")
        return value
```

Add to `.env.example` after `AI_TIMEOUT_SECONDS=30`:

```dotenv
CHAT_MEMORY_PATH=data/chat_memory.sqlite3
CHAT_MEMORY_RETENTION_DAYS=3
CHAT_MEMORY_DEFAULT_TURNS=10
CHAT_MEMORY_MAX_RESULTS=20
```

- [ ] **Step 4: Run config tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -v
.\.venv\Scripts\python -m ruff check src/qq_bot/config.py tests/test_config.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git add src/qq_bot/config.py tests/test_config.py .env.example
git commit -m "feat: add chat memory settings"
```

---

### Task 2: SQLite Chat Memory Service

**Files:**
- Create: `src/qq_bot/services/chat_memory.py`
- Create: `tests/test_chat_memory.py`

- [ ] **Step 1: Write failing memory service tests**

Create `tests/test_chat_memory.py`:

```python
from datetime import datetime, timedelta, timezone

from qq_bot.services.chat_memory import ChatMemoryStore


def test_add_message_initializes_database_and_reads_user_history(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    message_id = store.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 你好",
        is_ai_prompt=True,
        created_at=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
    )
    store.update_ai_reply(message_id, "你好呀")
    store.add_message(
        group_id=1001,
        user_id=2002,
        message_text="别人的消息",
        created_at=datetime(2026, 5, 11, 12, 1, tzinfo=timezone.utc),
    )

    rows = store.recent_user_turns(group_id=1001, user_id=2001, limit=10)

    assert len(rows) == 1
    assert rows[0].message_text == "ai 你好"
    assert rows[0].ai_reply == "你好呀"
    assert rows[0].is_ai_prompt is True


def test_recent_group_messages_returns_newest_limited_rows_in_chronological_order(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    base = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    for index in range(5):
        store.add_message(
            group_id=1001,
            user_id=2001 + index,
            message_text=f"消息{index}",
            created_at=base + timedelta(minutes=index),
        )

    rows = store.recent_group_messages(group_id=1001, limit=3)

    assert [row.message_text for row in rows] == ["消息2", "消息3", "消息4"]


def test_search_group_messages_filters_keyword_and_user(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    created_at = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    store.add_message(1001, 2001, "洛克王国 迪莫", created_at=created_at)
    store.add_message(1001, 2002, "洛克王国 火花", created_at=created_at)
    store.add_message(1001, 2001, "别的话题", created_at=created_at)

    rows = store.search_messages(group_id=1001, keyword="洛克", user_id=2001, limit=10)

    assert [row.message_text for row in rows] == ["洛克王国 迪莫"]


def test_cleanup_removes_records_older_than_retention(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    store.add_message(1001, 2001, "旧消息", created_at=now - timedelta(days=4), now=now)
    store.add_message(1001, 2001, "新消息", created_at=now - timedelta(days=1), now=now)

    rows = store.recent_group_messages(group_id=1001, limit=10, now=now)

    assert [row.message_text for row in rows] == ["新消息"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_chat_memory.py -v
```

Expected: FAIL because `qq_bot.services.chat_memory` does not exist.

- [ ] **Step 3: Implement the service**

Create `src/qq_bot/services/chat_memory.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class ChatMemoryRow:
    id: int
    group_id: int
    user_id: int
    message_text: str
    created_at: str
    is_ai_prompt: bool
    ai_reply: str


class ChatMemoryStore:
    def __init__(self, path: str | Path, *, retention_days: int) -> None:
        self.path = Path(path)
        self.retention_days = retention_days

    def add_message(
        self,
        group_id: int,
        user_id: int,
        message_text: str,
        is_ai_prompt: bool = False,
        created_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        self._ensure_parent_dir()
        timestamp = _to_iso(created_at or _utc_now())
        with self._connect() as connection:
            self._initialize(connection)
            self._cleanup(connection, now=now)
            cursor = connection.execute(
                """
                INSERT INTO chat_messages
                    (group_id, user_id, message_text, created_at, is_ai_prompt, ai_reply)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (group_id, user_id, message_text, timestamp, int(is_ai_prompt)),
            )
            return int(cursor.lastrowid)

    def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        self._ensure_parent_dir()
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute(
                "UPDATE chat_messages SET ai_reply = ? WHERE id = ?",
                (ai_reply, message_id),
            )

    def recent_user_turns(
        self,
        *,
        group_id: int,
        user_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        return self._query(
            """
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE group_id = ? AND user_id = ? AND is_ai_prompt = 1
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) ORDER BY created_at ASC, id ASC
            """,
            (group_id, user_id, limit),
            now=now,
        )

    def recent_group_messages(
        self,
        *,
        group_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        return self._query(
            """
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE group_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) ORDER BY created_at ASC, id ASC
            """,
            (group_id, limit),
            now=now,
        )

    def search_messages(
        self,
        *,
        group_id: int,
        keyword: str | None = None,
        user_id: int | None = None,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        clauses = ["group_id = ?"]
        params: list[object] = [group_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if keyword:
            clauses.append("message_text LIKE ?")
            params.append(f"%{keyword}%")
        params.append(limit)
        return self._query(
            f"""
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) ORDER BY created_at ASC, id ASC
            """,
            tuple(params),
            now=now,
        )

    def _query(
        self,
        sql: str,
        params: tuple[object, ...],
        *,
        now: datetime | None,
    ) -> list[ChatMemoryRow]:
        self._ensure_parent_dir()
        with self._connect() as connection:
            self._initialize(connection)
            self._cleanup(connection, now=now)
            rows = connection.execute(sql, params).fetchall()
        return [_row_from_sqlite(row) for row in rows]

    def _ensure_parent_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_ai_prompt INTEGER NOT NULL DEFAULT 0,
                ai_reply TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_group_user_time "
            "ON chat_messages(group_id, user_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_group_time "
            "ON chat_messages(group_id, created_at)"
        )

    def _cleanup(self, connection: sqlite3.Connection, *, now: datetime | None) -> None:
        cutoff = _to_iso((now or _utc_now()) - timedelta(days=self.retention_days))
        connection.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))


def _row_from_sqlite(row: sqlite3.Row) -> ChatMemoryRow:
    return ChatMemoryRow(
        id=int(row["id"]),
        group_id=int(row["group_id"]),
        user_id=int(row["user_id"]),
        message_text=str(row["message_text"]),
        created_at=str(row["created_at"]),
        is_ai_prompt=bool(row["is_ai_prompt"]),
        ai_reply=str(row["ai_reply"]),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
```

- [ ] **Step 4: Run service tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_chat_memory.py -v
.\.venv\Scripts\python -m ruff check src/qq_bot/services/chat_memory.py tests/test_chat_memory.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add src/qq_bot/services/chat_memory.py tests/test_chat_memory.py
git commit -m "feat: add sqlite chat memory store"
```

---

### Task 3: Memory Prompt Parser and Formatter

**Files:**
- Create: `src/qq_bot/services/memory_prompt.py`
- Create: `tests/test_memory_prompt.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_memory_prompt.py`:

```python
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import (
    MemoryReference,
    extract_at_user_ids,
    format_chat_context,
    parse_memory_reference,
)


def test_parse_recent_reference() -> None:
    parsed = parse_memory_reference("参考最近20条：继续总结", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="继续总结", limit=20)


def test_parse_keyword_reference() -> None:
    parsed = parse_memory_reference("参考 洛克王国 的聊天：我们之前说了什么", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="我们之前说了什么", keyword="洛克王国")


def test_parse_mentioned_user_recent_reference() -> None:
    parsed = parse_memory_reference("参考 @小明 的最近20条：总结他的想法", mentioned_user_ids=[2001])

    assert parsed == MemoryReference(question="总结他的想法", user_id=2001, limit=20)


def test_parse_mentioned_user_keyword_reference() -> None:
    parsed = parse_memory_reference("参考 @小明 关于 洛克王国 的聊天：整理重点", mentioned_user_ids=[2001])

    assert parsed == MemoryReference(question="整理重点", user_id=2001, keyword="洛克王国")


def test_parse_non_reference_keeps_prompt_as_question() -> None:
    parsed = parse_memory_reference("讲个笑话", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="讲个笑话")


def test_extract_at_user_ids_from_message_segments() -> None:
    class FakeSegment:
        def __init__(self, segment_type: str, qq: str) -> None:
            self.type = segment_type
            self.data = {"qq": qq}

    assert extract_at_user_ids([FakeSegment("at", "2001"), FakeSegment("text", "ignored")]) == [2001]


def test_format_chat_context_includes_messages_and_ai_replies() -> None:
    rows = [
        ChatMemoryRow(1, 1001, 2001, "ai 你好", "2026-05-11T12:00:00+00:00", True, "你好呀"),
        ChatMemoryRow(2, 1001, 2002, "洛克王国", "2026-05-11T12:01:00+00:00", False, ""),
    ]

    context = format_chat_context(rows)

    assert "用户2001：ai 你好" in context
    assert "机器人：你好呀" in context
    assert "用户2002：洛克王国" in context


def test_format_chat_context_reports_empty_history() -> None:
    assert format_chat_context([]) == "没有找到相关历史聊天记录。"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_memory_prompt.py -v
```

Expected: FAIL because `qq_bot.services.memory_prompt` does not exist.

- [ ] **Step 3: Implement parser and formatter**

Create `src/qq_bot/services/memory_prompt.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from qq_bot.services.chat_memory import ChatMemoryRow


class MessageSegmentLike(Protocol):
    type: str
    data: dict[str, object]


@dataclass(frozen=True)
class MemoryReference:
    question: str
    user_id: int | None = None
    keyword: str | None = None
    limit: int | None = None


def extract_at_user_ids(segments: Iterable[MessageSegmentLike]) -> list[int]:
    user_ids: list[int] = []
    for segment in segments:
        if segment.type != "at":
            continue
        qq = segment.data.get("qq")
        try:
            user_ids.append(int(str(qq)))
        except (TypeError, ValueError):
            continue
    return user_ids


def parse_memory_reference(prompt: str, *, mentioned_user_ids: list[int]) -> MemoryReference:
    text = prompt.strip()
    if not text.startswith("参考"):
        return MemoryReference(question=text)

    head, separator, question = _split_reference(text)
    if not separator:
        return MemoryReference(question=text)

    user_id = mentioned_user_ids[0] if "@" in head and mentioned_user_ids else None
    limit = _extract_limit(head)
    keyword = _extract_keyword(head)
    return MemoryReference(
        question=question.strip(),
        user_id=user_id,
        keyword=keyword,
        limit=limit,
    )


def format_chat_context(rows: list[ChatMemoryRow]) -> str:
    if not rows:
        return "没有找到相关历史聊天记录。"

    lines = ["历史聊天记录："]
    for row in rows:
        lines.append(f"用户{row.user_id}：{row.message_text}")
        if row.ai_reply:
            lines.append(f"机器人：{row.ai_reply}")
    return "\n".join(lines)


def _split_reference(text: str) -> tuple[str, str, str]:
    for separator in ("：", ":"):
        if separator in text:
            head, question = text.split(separator, 1)
            return head.strip(), separator, question.strip()
    return text, "", ""


def _extract_limit(head: str) -> int | None:
    match = re.search(r"最近\s*(\d+)\s*条", head)
    if not match:
        return None
    return int(match.group(1))


def _extract_keyword(head: str) -> str | None:
    about_match = re.search(r"关于\s*(.+?)\s*的聊天", head)
    if about_match:
        return about_match.group(1).strip()

    keyword_match = re.search(r"参考\s*(.+?)\s*的聊天", head)
    if keyword_match:
        keyword = keyword_match.group(1).strip()
        if "@" not in keyword and "最近" not in keyword:
            return keyword
    return None
```

- [ ] **Step 4: Run parser tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_memory_prompt.py -v
.\.venv\Scripts\python -m ruff check src/qq_bot/services/memory_prompt.py tests/test_memory_prompt.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
git add src/qq_bot/services/memory_prompt.py tests/test_memory_prompt.py
git commit -m "feat: parse ai memory references"
```

---

### Task 4: AI Payload Chat Context

**Files:**
- Modify: `src/qq_bot/services/ai_client.py`
- Test: `tests/test_ai_client.py`

- [ ] **Step 1: Write failing AI client tests**

Append to `tests/test_ai_client.py`:

```python
def test_build_chat_payload_includes_chat_context_when_provided() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "继续刚才的话题",
        settings,
        chat_context="历史聊天记录：\n用户2001：ai 你好\n机器人：你好呀",
    )

    user_message = payload["messages"][-1]["content"]
    system_prompt = payload["messages"][0]["content"]
    assert "当前用户问题：继续刚才的话题" in user_message
    assert "历史聊天记录" in user_message
    assert "不要编造不存在的历史聊天记录" in system_prompt


def test_build_chat_payload_combines_search_and_chat_context() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "这事现在怎么样",
        settings,
        search_context="[1] News\nURL: https://example.com\n摘要: summary",
        chat_context="历史聊天记录：\n用户2001：之前说过 DeepSeek",
    )

    user_message = payload["messages"][-1]["content"]
    assert "当前用户问题：这事现在怎么样" in user_message
    assert "联网搜索资料" in user_message
    assert "历史聊天记录" in user_message
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
```

Expected: FAIL because `build_chat_payload()` does not accept `chat_context`.

- [ ] **Step 3: Implement `chat_context` support**

In `src/qq_bot/services/ai_client.py`, update signatures:

```python
def build_chat_payload(
    prompt: str,
    settings: BotSettings,
    *,
    search_context: str = "",
    chat_context: str = "",
    current_time: str | None = None,
) -> dict[str, Any]:
```

```python
async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    search_context: str = "",
    chat_context: str = "",
) -> str:
```

Replace the `user_content` building block with this structure:

```python
    user_sections = [f"当前用户问题：{cleaned_prompt}"]
    cleaned_search_context = search_context.strip()
    cleaned_chat_context = chat_context.strip()
    if cleaned_chat_context:
        system_prompt += (
            " 如果提供了历史聊天记录，只把它作为理解前文和用户意图的参考。"
            "不要编造不存在的历史聊天记录；历史不足时要直接说明。"
        )
        user_sections.append(f"历史聊天记录：\n{cleaned_chat_context}")
    if cleaned_search_context:
        system_prompt += (
            " 如果提供了联网搜索资料，请优先依据资料回答；"
            "不要编造资料外的信息，不要编造链接，不要编造时间，不要编造价格。"
            "如果搜索资料不足或互相冲突，就说没有可靠来源或信息不一致。"
            "回复末尾加“来源：”，最多 3 条，格式为“1. 标题 - URL”。"
        )
        user_sections.append(f"联网搜索资料：\n{cleaned_search_context}")
    user_content = "\n\n".join(user_sections)
```

Keep compatibility with existing tests by allowing the plain no-context case to remain a simple final user content if needed:

```python
    if not cleaned_search_context and not cleaned_chat_context:
        user_content = cleaned_prompt
```

Pass `chat_context` into `build_chat_payload()` inside `request_ai_reply()`:

```python
            json=build_chat_payload(
                prompt,
                active_settings,
                search_context=search_context,
                chat_context=chat_context,
            ),
```

- [ ] **Step 4: Run AI client tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_client.py -v
.\.venv\Scripts\python -m ruff check src/qq_bot/services/ai_client.py tests/test_ai_client.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
git add src/qq_bot/services/ai_client.py tests/test_ai_client.py
git commit -m "feat: include chat history in ai payloads"
```

---

### Task 5: Wire Memory Into AI Chat Plugin

**Files:**
- Modify: `src/qq_bot/plugins/ai_chat.py`
- Test: `tests/test_ai_chat_plugin.py`

- [ ] **Step 1: Extend fake event test helper**

Update `FakeEvent` in `tests/test_ai_chat_plugin.py` so it has a user ID and iterable message segments:

```python
class FakeEvent:
    group_id = 1001
    user_id = 2001

    def __init__(self, text: str, segments: list[object] | None = None):
        self.text = text
        self.segments = segments or []

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(self.segments)

    def is_tome(self) -> bool:
        return False
```

- [ ] **Step 2: Write failing plugin tests**

Append to `tests/test_ai_chat_plugin.py`:

```python
@pytest.mark.asyncio
async def test_ai_chat_passes_default_group_user_memory_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            assert message_id == 123
            assert ai_reply == "带记忆回复"

        def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
            assert group_id == 1001
            assert user_id == 2001
            assert limit == 10
            return []

    async def fake_request_ai_reply(prompt: str, *, settings: BotSettings, search_context: str = "", chat_context: str = "") -> str:
        assert prompt == "继续"
        assert chat_context == "没有找到相关历史聊天记录。"
        return "带记忆回复"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"))
    monkeypatch.setattr(ai_chat_plugin, "ChatMemoryStore", lambda path, retention_days: FakeStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 继续"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_uses_explicit_recent_group_history(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 5
            return []

    async def fake_request_ai_reply(prompt: str, *, settings: BotSettings, search_context: str = "", chat_context: str = "") -> str:
        assert prompt == "总结"
        assert chat_context == "没有找到相关历史聊天记录。"
        return "总结好了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"))
    monkeypatch.setattr(ai_chat_plugin, "ChatMemoryStore", lambda path, retention_days: FakeStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 参考最近5条：总结"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_memory_failure_does_not_block_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenStore:
        def add_message(self, *args, **kwargs) -> int:
            raise OSError("database locked")

        def recent_user_turns(self, *args, **kwargs):
            raise OSError("database locked")

    async def fake_request_ai_reply(prompt: str, *, settings: BotSettings, search_context: str = "", chat_context: str = "") -> str:
        assert prompt == "你好"
        assert chat_context == ""
        return "你好"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"))
    monkeypatch.setattr(ai_chat_plugin, "ChatMemoryStore", lambda path, retention_days: BrokenStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_chat_plugin.py -v
```

Expected: FAIL because `ai_chat.py` does not use `ChatMemoryStore` or pass `chat_context`.

- [ ] **Step 4: Implement plugin wiring**

Update imports in `src/qq_bot/plugins/ai_chat.py`:

```python
from qq_bot.services.chat_memory import ChatMemoryStore
from qq_bot.services.memory_prompt import (
    extract_at_user_ids,
    format_chat_context,
    parse_memory_reference,
)
```

Inside `handle_ai_chat()` after settings and allowed-group checks, construct the store:

```python
    memory_store = ChatMemoryStore(
        settings.chat_memory_path,
        retention_days=settings.chat_memory_retention_days,
    )
```

After `raw_text` is computed and before returning on non-AI messages, record the message safely:

```python
    memory_message_id: int | None = None
    is_ai_prompt = prompt is not None or event.is_tome()
    try:
        memory_message_id = memory_store.add_message(
            group_id=event.group_id,
            user_id=event.user_id,
            message_text=raw_text,
            is_ai_prompt=is_ai_prompt,
        )
    except Exception:
        logger.exception("Chat memory write failed; continuing without storing message")
```

If the message is not an AI prompt, keep the existing `return` behavior after recording it.

Before search and `request_ai_reply()`, parse memory intent and build context:

```python
    mentioned_user_ids = extract_at_user_ids(event.get_message())
    memory_reference = parse_memory_reference(prompt, mentioned_user_ids=mentioned_user_ids)
    prompt = memory_reference.question

    chat_context = ""
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
```

Pass `chat_context=chat_context` to `request_ai_reply()`.

After the reply is produced and before `finish()`, update the stored AI reply safely:

```python
    if memory_message_id is not None:
        try:
            memory_store.update_ai_reply(memory_message_id, reply)
        except Exception:
            logger.exception("Chat memory reply update failed")
```

- [ ] **Step 5: Run plugin tests and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_ai_chat_plugin.py -v
.\.venv\Scripts\python -m ruff check src/qq_bot/plugins/ai_chat.py tests/test_ai_chat_plugin.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 6: Commit Task 5**

Run:

```powershell
git add src/qq_bot/plugins/ai_chat.py tests/test_ai_chat_plugin.py
git commit -m "feat: wire chat memory into ai replies"
```

---

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README configuration example**

In `README.md`, add these lines after `AI_TIMEOUT_SECONDS=30` in the dotenv example:

```dotenv
CHAT_MEMORY_PATH=data/chat_memory.sqlite3
CHAT_MEMORY_RETENTION_DAYS=3
CHAT_MEMORY_DEFAULT_TURNS=10
CHAT_MEMORY_MAX_RESULTS=20
```

- [ ] **Step 2: Document AI memory behavior**

Add this paragraph after the existing search explanation:

```markdown
AI chat keeps a local 3-day SQLite memory at `CHAT_MEMORY_PATH`. Normal AI prompts automatically use recent context for the current group user. You can explicitly ask it to reference group history with prompts such as `ai 参考最近20条：总结一下`, `ai 参考 洛克王国 的聊天：我们之前说了什么`, or `ai 参考 @某人 的最近20条：总结他的想法`.
```

- [ ] **Step 3: Update manual verification**

Add these manual checks under `## Manual Verification`:

```markdown
- Send `ai 我喜欢迪莫`, then send `ai 我刚才说我喜欢谁` from the same QQ user in the same group, and expect the reply to use the recent context.
- Send several normal group messages, then send `ai 参考最近5条：总结一下`, and expect the reply to reference those recent group messages.
- Send `ai 参考 @某人 的最近5条：总结他的观点`, and expect the reply to use that mentioned user's messages when present.
```

- [ ] **Step 4: Run full test suite and lint**

Run:

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\python -m ruff check .
```

Expected: all tests PASS and `All checks passed!`.

- [ ] **Step 5: Commit Task 6**

Run:

```powershell
git add README.md
git commit -m "docs: document ai chat memory"
```

---

## Implementation Notes

- Use only local SQLite; do not add third-party dependencies for persistence.
- Do not store secrets in the database or documentation.
- Keep memory failures non-fatal so AI chat still works if the database is locked or unavailable.
- The first implementation should use simple `LIKE` keyword search because it is sufficient for 3-day local retention.
- Preserve existing search behavior: web search context is for current facts, chat context is for conversation continuity.

## Self-Review

- Spec coverage: storage, capture, default context, explicit recent/keyword/@user references, AI payload, non-fatal errors, tests, docs, and 3-day retention are each covered by a task.
- Incomplete-marker scan: no unfinished task markers remain; every step includes file paths, commands, expected outcomes, and concrete code snippets.
- Type consistency: `ChatMemoryStore`, `ChatMemoryRow`, `MemoryReference`, `chat_context`, and configuration names are consistent across tasks.
