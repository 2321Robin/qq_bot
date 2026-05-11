import pytest
from nonebot.adapters.onebot.v11 import Message

from qq_bot.config import BotSettings
from qq_bot.plugins import ai_chat as ai_chat_plugin
from qq_bot.services.chat_memory import ChatMemoryRow


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


class FakeAtSegment:
    type = "at"

    def __init__(self, qq: int):
        self.data = {"qq": str(qq)}


class FakeTextSegment:
    type = "text"

    def __init__(self, text: str):
        self.data = {"text": text}


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


class EmptyMemoryStore:
    def add_message(self, *args, **kwargs) -> int:
        return 123

    def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        return None

    def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
        return []


def memory_row(
    *,
    message_text: str,
    ai_reply: str = "",
    row_id: int = 1,
    user_id: int = 2001,
) -> ChatMemoryRow:
    return ChatMemoryRow(
        id=row_id,
        group_id=1001,
        user_id=user_id,
        message_text=message_text,
        created_at="2026-05-11T00:00:00+00:00",
        is_ai_prompt=True,
        ai_reply=ai_reply,
    )


@pytest.mark.asyncio
async def test_ai_chat_formats_named_mentions_in_final_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "提醒我"
        assert settings.ai_api_key == "secret"
        assert search_context == ""
        assert chat_context == "没有找到相关历史聊天记录。"
        return "好的，@小呱呱 会收到提醒"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]

    message = exc_info.value.message
    assert isinstance(message, Message)
    assert message[0].type == "text"
    assert message[0].data["text"] == "好的，"
    assert message[1].type == "at"
    assert message[1].data["qq"] == "2854203313"
    assert message[2].type == "text"
    assert message[2].data["text"] == " 会收到提醒"


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
        chat_context: str = "",
    ) -> str:
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert "DeepSeek News" in search_context
        assert "https://example.com/news" in search_context
        assert chat_context == "没有找到相关历史聊天记录。"
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
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: EmptyMemoryStore(),
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
        chat_context: str = "",
    ) -> str:
        assert prompt == "讲个笑话"
        assert search_context == ""
        assert chat_context == "没有找到相关历史聊天记录。"
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
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: EmptyMemoryStore(),
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
        chat_context: str = "",
    ) -> str:
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert search_context == ""
        assert chat_context == "没有找到相关历史聊天记录。"
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
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天 DeepSeek 有什么新闻"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_passes_default_group_user_memory_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return [memory_row(message_text="之前的问题", ai_reply="之前的回答")]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "继续"
        assert "用户2001：之前的问题" in chat_context
        assert "机器人：之前的回答" in chat_context
        return "带记忆回复"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 继续"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_uses_explicit_recent_group_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 5
            return []

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "总结"
        assert chat_context == "没有找到相关历史聊天记录。"
        return "总结好了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 参考最近5条：总结"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_uses_actual_at_segment_for_explicit_user_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        def search_messages(
            self,
            *,
            group_id: int,
            user_id: int | None = None,
            keyword: str | None = None,
            limit: int,
        ):
            assert group_id == 1001
            assert user_id == 2002
            assert keyword is None
            assert limit == 5
            return [memory_row(message_text="他的观点", user_id=2002)]

        def recent_group_messages(self, *args, **kwargs):
            raise AssertionError("should not use group history for explicit at reference")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "总结他的观点"
        assert "用户2002：他的观点" in chat_context
        return "总结好了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "ai 参考  的最近5条：总结他的观点",
                [
                    FakeTextSegment("ai 参考 "),
                    FakeAtSegment(2002),
                    FakeTextSegment(" 的最近5条：总结他的观点"),
                ],
            )
        )


@pytest.mark.asyncio
async def test_ai_chat_does_not_scope_group_history_to_at_after_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 5
            return [memory_row(message_text="群聊观点", user_id=2003)]

        def search_messages(self, *args, **kwargs):
            raise AssertionError("question mention should not scope group history")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "你怎么看"
        assert "用户2003：群聊观点" in chat_context
        return "总结好了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "ai 参考最近5条： 你怎么看",
                [
                    FakeTextSegment("ai 参考最近5条："),
                    FakeAtSegment(2002),
                    FakeTextSegment(" 你怎么看"),
                ],
            )
        )


@pytest.mark.asyncio
async def test_ai_chat_memory_failure_does_not_block_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStore:
        def add_message(self, *args, **kwargs) -> int:
            raise OSError("database locked")

        def recent_user_turns(self, *args, **kwargs):
            raise OSError("database locked")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "你好"
        assert chat_context == ""
        return "你好"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: BrokenStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_memory_store_construction_failure_does_not_block_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_store(path, retention_days):
        raise OSError("cannot initialize database")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "你好"
        assert chat_context == ""
        return "你好"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "ChatMemoryStore", broken_store)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_records_non_ai_group_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []

    class FakeStore:
        def add_message(self, **kwargs) -> int:
            recorded.append(kwargs)
            return 123

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )

    await ai_chat_plugin.handle_ai_chat(FakeEvent("普通聊天"))  # type: ignore[arg-type]

    assert recorded == [
        {
            "group_id": 1001,
            "user_id": 2001,
            "message_text": "普通聊天",
            "is_ai_prompt": False,
        }
    ]


@pytest.mark.asyncio
async def test_ai_chat_excludes_current_prompt_from_memory_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.rows = [memory_row(message_text="旧问题", row_id=1)]

        def add_message(self, *, message_text: str, **kwargs) -> int:
            self.rows.append(memory_row(message_text=message_text, row_id=2))
            return 2

        def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
            return self.rows[-limit:]

    store = FakeStore()

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        assert prompt == "继续"
        assert "旧问题" in chat_context
        assert "ai 继续" not in chat_context
        return "好的"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "ChatMemoryStore", lambda path, retention_days: store)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 继续"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_rejects_empty_question_after_memory_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def add_message(self, *args, **kwargs) -> int:
            return 123

        def recent_group_messages(self, *, group_id: int, limit: int):
            return []

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        search_context: str = "",
        chat_context: str = "",
    ) -> str:
        raise AssertionError("AI should not be called")

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "ChatMemoryStore",
        lambda path, retention_days: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 参考最近5条："))  # type: ignore[arg-type]

    assert exc_info.value.message == "请输入要问的问题"
