import asyncio
from datetime import UTC, datetime

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException

from qq_bot.config import BotSettings
from qq_bot.plugins import ai_chat as ai_chat_plugin
from qq_bot.runtime import RuntimeStateError
from qq_bot.services import roco_knowledge
from qq_bot.services.chat_memory import ChatMemoryRow


class FakeEvent:
    group_id = 1001
    user_id = 2001

    def __init__(
        self,
        text: str,
        segments: list[object] | None = None,
        *,
        to_me: bool = False,
        self_id: int = 2880000001,
        user_id: int | None = None,
    ):
        self.text = text
        self.segments = segments or []
        self.to_me = to_me
        self.self_id = self_id
        if user_id is not None:
            self.user_id = user_id

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(self.segments)

    def is_tome(self) -> bool:
        return self.to_me


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
    async def add_message(self, *args, **kwargs) -> int:
        return 123

    async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        return None

    async def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
        return []

    async def recent_group_messages(self, *, group_id: int, limit: int):
        return []


class FakeHttpClient:
    """Stand-in for the runtime-owned shared httpx client."""


@pytest.fixture(autouse=True)
def _patch_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime is not started in these tests; stub the client getter."""
    monkeypatch.setattr(ai_chat_plugin, "get_http_client", lambda: FakeHttpClient())


@pytest.fixture(autouse=True)
async def _cancel_leftover_tasks():
    """被 @ 路径会调度真实的冷场检查 task（默认沉睡 180s）；测试结束时取消
    仍未完成的 task，避免事件循环关闭时出现 "Task was destroyed" 告警。"""
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


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
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            named_mention_replacements="@小呱呱=2880000001",
        ),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
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
    assert message[1].data["qq"] == "2880000001"
    assert message[2].type == "text"
    assert message[2].data["text"] == " 会收到提醒"


@pytest.mark.asyncio
async def test_ai_chat_replies_when_group_message_mentions_bot_without_to_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "你好"
        return "你好呀"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "你好",
                [FakeAtSegment(2880000001), FakeTextSegment(" 你好")],
                to_me=False,
            )
        )

    message = exc_info.value.message
    assert isinstance(message, Message)
    assert message.extract_plain_text() == "你好呀"


@pytest.mark.asyncio
async def test_ai_chat_ignores_configured_sender_even_when_message_mentions_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            raise AssertionError("ignored sender should not be recorded")

    async def fake_request_ai_reply(*args, **kwargs) -> str:
        raise AssertionError("AI should not be called for ignored sender")

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            ai_ignored_user_ids="2880000002",
        ),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)

    await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
        FakeEvent(
            "投票成功\n今日票数：81\n总票数：7196",
            [FakeAtSegment(2880000001), FakeTextSegment(" 投票成功")],
            to_me=True,
            self_id=2880000001,
            user_id=2880000002,
        )
    )


@pytest.mark.asyncio
async def test_ai_chat_ignores_self_mention_for_explicit_user_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def search_messages(
            self,
            *,
            group_id: int,
            user_id: int | None = None,
            keyword: str | None = None,
            limit: int,
        ):
            assert user_id == 2002
            return [memory_row(message_text="他的观点", user_id=2002)]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "参考  的最近5条：总结他的观点",
                [
                    FakeAtSegment(2880000001),
                    FakeTextSegment(" 参考 "),
                    FakeAtSegment(2002),
                    FakeTextSegment(" 的最近5条：总结他的观点"),
                ],
                to_me=False,
            )
        )


@pytest.mark.asyncio
async def test_ai_chat_uses_search_context_for_search_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.search import SearchResult

    async def fake_search_web(prompt: str, *, settings: BotSettings, client: object | None = None):
        assert prompt == "今天 DeepSeek 有什么新闻"
        assert settings.tavily_api_key == "tvly-secret"
        return [SearchResult("DeepSeek News", "https://example.com/news", "news summary")]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
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
    async def fake_search_web(prompt: str, *, settings: BotSettings, client: object | None = None):
        raise AssertionError("search should not be called")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "讲个笑话"
        assert search_context == ""
        assert chat_context == "没有找到相关历史聊天记录。"
        assert roco_context == ""
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
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
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

    async def fake_search_web(prompt: str, *, settings: BotSettings, client: object | None = None):
        raise SearchError("search down")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天 DeepSeek 有什么新闻"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_refuses_current_events_without_search_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_ai_reply(*args, **kwargs) -> str:
        raise AssertionError("AI should not answer current-event prompts without search")

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=False,
            tavily_api_key="",
        ),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天有什么新闻"))  # type: ignore[arg-type]

    assert "需要联网搜索" in str(exc_info.value.message)


@pytest.mark.asyncio
async def test_ai_chat_refuses_current_events_when_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.search import SearchError

    async def fake_search_web(prompt: str, *, settings: BotSettings, client: object | None = None):
        raise SearchError("search down")

    async def fake_request_ai_reply(*args, **kwargs) -> str:
        raise AssertionError("AI should not answer current-event prompts when search fails")

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
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天 DeepSeek 有什么新闻"))  # type: ignore[arg-type]

    assert "联网搜索失败" in str(exc_info.value.message)


@pytest.mark.asyncio
async def test_ai_chat_passes_roco_context_for_roco_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "画精灵怎么进化？"
        assert search_context == ""
        assert chat_context == "没有找到相关历史聊天记录。"
        assert roco_context == "问题类型：进化\n匹配精灵：画精灵"
        return "画像守护"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    def fake_build_roco_context(prompt: str) -> str:
        assert prompt == "画精灵怎么进化？"
        return "问题类型：进化\n匹配精灵：画精灵"

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(roco_knowledge, "build_roco_context", fake_build_roco_context)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 画精灵怎么进化？"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_legacy_path_skips_roco_context_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_spans: list[str] = []

    class RecordingTracer:
        def start_span(self, name: str, *, trace_id: str, **attributes: str) -> object:
            started_spans.append(name)
            return object()

        def end_span(self, span: object, *, status: str = "ok", **attributes: str) -> None:
            return None

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "画精灵怎么进化？"
        assert roco_context == ""
        return "直答"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            roco_enabled=False,
        ),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: EmptyMemoryStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_tracer", lambda: RecordingTracer())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 画精灵怎么进化？"))  # type: ignore[arg-type]

    assert "knowledge.lookup" not in started_spans
    # 对照：同一请求其余阶段的 span 照常记录，证明 tracer 已接线。
    assert "memory.retrieve" in started_spans


@pytest.mark.asyncio
async def test_ai_chat_passes_default_group_memory_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            assert message_id == 123
            assert ai_reply == "带记忆回复"

        async def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 10
            return [memory_row(message_text="之前的问题", ai_reply="之前的回答", user_id=2002)]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "继续"
        assert "用户2002：之前的问题" in chat_context
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
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 继续"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_uses_recent_group_messages_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"recent_group_messages": False, "recent_user_turns": False}

    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def recent_group_messages(self, *, group_id: int, limit: int):
            calls["recent_group_messages"] = True
            assert group_id == 1001
            assert limit == 10
            return [
                memory_row(message_text="普通群友发言", user_id=2002),
                memory_row(message_text="另一个群友发言", row_id=2, user_id=2003),
            ]

        async def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
            calls["recent_user_turns"] = True
            return []

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "刚才大家在说什么"
        assert "用户2002：普通群友发言" in chat_context
        assert "用户2003：另一个群友发言" in chat_context
        return "他们在聊天"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 刚才大家在说什么"))  # type: ignore[arg-type]

    message = exc_info.value.message
    assert isinstance(message, Message)
    assert message.extract_plain_text() == "他们在聊天"
    assert calls["recent_group_messages"] is True
    assert calls["recent_user_turns"] is False


@pytest.mark.asyncio
async def test_ai_chat_uses_explicit_recent_group_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 5
            return []

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: FakeStore(),
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
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def search_messages(
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

        async def recent_group_messages(self, *args, **kwargs):
            raise AssertionError("should not use group history for explicit at reference")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: FakeStore(),
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
async def test_ai_chat_summarizes_mentioned_user_recent_messages_in_natural_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def search_messages(
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
            assert limit == 3
            return [memory_row(message_text="目标用户的信息", user_id=2002)]

        async def recent_user_turns(self, *args, **kwargs):
            raise AssertionError("should not use sender history for mentioned-user summary")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "总结 最近三条消息"
        assert "用户2002：目标用户的信息" in chat_context
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
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "总结  最近三条消息",
                [
                    FakeAtSegment(2880000001),
                    FakeTextSegment(" 总结 "),
                    FakeAtSegment(2002),
                    FakeTextSegment(" 最近三条消息"),
                ],
                to_me=False,
            )
        )


@pytest.mark.asyncio
async def test_ai_chat_does_not_scope_group_history_to_at_after_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def recent_group_messages(self, *, group_id: int, limit: int):
            assert group_id == 1001
            assert limit == 5
            return [memory_row(message_text="群聊观点", user_id=2003)]

        async def search_messages(self, *args, **kwargs):
            raise AssertionError("question mention should not scope group history")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: FakeStore(),
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
        async def add_message(self, *args, **kwargs) -> int:
            raise OSError("database locked")

        async def recent_group_messages(self, *args, **kwargs):
            raise OSError("database locked")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: BrokenStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_memory_store_construction_failure_does_not_block_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_repository():
        raise OSError("cannot open database")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", broken_repository)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_records_ordinary_group_message_without_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []
    request_ai_reply_called = False
    finish_called = False

    class FakeStore:
        async def add_message(self, **kwargs) -> int:
            recorded.append(kwargs)
            return 123

    async def fake_request_ai_reply(*args, **kwargs) -> str:
        nonlocal request_ai_reply_called
        request_ai_reply_called = True
        raise AssertionError("AI should not be called for ordinary group messages")

    async def fake_finish(message: object) -> None:
        nonlocal finish_called
        finish_called = True
        raise AssertionError("finish should not be called for ordinary group messages")

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("普通聊天内容"))  # type: ignore[arg-type]

    assert recorded == [
        {
            "group_id": 1001,
            "user_id": 2001,
            "message_text": "普通聊天内容",
            "is_ai_prompt": False,
        }
    ]
    assert request_ai_reply_called is False
    assert finish_called is False


@pytest.mark.asyncio
async def test_ai_chat_records_non_ai_group_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []

    class FakeStore:
        async def add_message(self, **kwargs) -> int:
            recorded.append(kwargs)
            return 123

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_chat_repository",
        lambda: FakeStore(),
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

        async def add_message(self, *, message_text: str, **kwargs) -> int:
            self.rows.append(memory_row(message_text=message_text, row_id=2))
            return 2

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def recent_group_messages(self, *, group_id: int, limit: int):
            return self.rows[-limit:]

    store = FakeStore()

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: store)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 继续"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_rejects_empty_question_after_memory_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def add_message(self, *args, **kwargs) -> int:
            return 123

        async def recent_group_messages(self, *, group_id: int, limit: int):
            return []

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
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
        "get_chat_repository",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 参考最近5条："))  # type: ignore[arg-type]

    assert exc_info.value.message == "请输入要问的问题"


@pytest.mark.asyncio
async def test_concurrent_events_reuse_same_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent events must share one repository instance, not create per-message stores."""

    class FakeStore:
        def __init__(self) -> None:
            self.writes: list[dict] = []

        async def add_message(self, *, message_text: str, **kwargs) -> int:
            self.writes.append({"message_text": message_text, **kwargs})
            return len(self.writes)

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            return None

        async def recent_group_messages(self, *, group_id: int, limit: int):
            return []

    store = FakeStore()
    repository_lookups = {"count": 0}

    def get_repository():
        repository_lookups["count"] += 1
        return store

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        return f"回复：{prompt[:2]}"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", get_repository)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    async def run_event() -> None:
        with pytest.raises(FinishCalled):
            await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 并发消息"))  # type: ignore[arg-type]

    await asyncio.gather(run_event(), run_event())

    assert repository_lookups["count"] == 2  # one lookup per event...
    assert len(store.writes) == 2  # ...but the same shared instance
    assert store.writes[0]["message_text"] == "ai 并发消息"
    assert store.writes[1]["message_text"] == "ai 并发消息"


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage-2 agent path (Task 13, S2-AGENT-09): AGENT_ENABLED routing
# ---------------------------------------------------------------------------


class FakeAgentRuntime:
    """Runtime stand-in exposing the agent-stack getters."""

    def __init__(self, orchestrator: object, gateway: object | None = None):
        self._orchestrator = orchestrator
        self._gateway = gateway

    def get_agent_orchestrator(self):
        return self._orchestrator

    def get_model_gateway(self):
        if self._gateway is None:
            raise RuntimeStateError("not ready")
        return self._gateway

    def get_quota_service(self):
        """Quota disabled in this fake: admission always allowed."""
        return None


class FakeOrchestrator:
    def __init__(self, outcome: object, store: object | None = None):
        self.outcome = outcome
        self.last_store = store
        self.runs: list[object] = []

    async def run(self, request: object) -> object:
        self.runs.append(request)
        return self.outcome


def _agent_settings(**overrides) -> BotSettings:
    values = {"allowed_group_ids": "1001", "ai_api_key": "secret", "agent_enabled": True}
    values.update(overrides)
    return BotSettings(**values)


def _patch_agent_runtime(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    monkeypatch.setattr(ai_chat_plugin, "get_runtime", lambda: runtime)


@pytest.mark.asyncio
async def test_agent_path_routes_renders_and_updates_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_ENABLED=true: router -> orchestrator -> rendered text; the
    memory write count stays exactly one add_message (S2-AGENT-09)."""
    from qq_bot.agent.evidence import EvidenceStore
    from qq_bot.agent.models import (
        Claim,
        Evidence,
        GroundedAnswer,
        ReasonCode,
        RouteDecision,
        RouteKind,
    )
    from qq_bot.agent.router import RouteTrace

    class Store:
        def __init__(self) -> None:
            self.writes: list[dict] = []

        async def add_message(self, **kwargs) -> int:
            self.writes.append(kwargs)
            return 1

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            self.writes.append({"reply": ai_reply})

    store = Store()
    answer = GroundedAnswer(
        claims=(Claim(text="暗影格斗是洛克王国精灵", kind="factual", evidence_ids=("L1",)),),
        closing="祝游戏愉快",
    )
    from qq_bot.agent.models import ToolResult

    evidence = EvidenceStore()
    evidence.add(
        ToolResult(
            tool="lookup_pet",
            status="ok",
            evidence=(
                Evidence(
                    id="L1", source_type="local", title="暗影格斗", facts={"name": "暗影格斗"}
                ),
            ),
        )
    )
    orchestrator = FakeOrchestrator(answer, store=evidence)

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.LOCAL_KNOWLEDGE,
            confidence=0.9,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            allowed_tools=("lookup_pet", "search_chat_memory"),
        )
        trace = RouteTrace(
            route=RouteKind.LOCAL_KNOWLEDGE,
            confidence=0.9,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            latency_ms=1.0,
            is_rule=True,
        )
        return decision, trace

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: store)
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 暗影格斗是谁"))  # type: ignore[arg-type]

    assert len(store.writes) == 2  # one add_message + one update_ai_reply
    assert store.writes[0]["message_text"] == "ai 暗影格斗是谁"
    assert store.writes[1] == {"reply": "暗影格斗是洛克王国精灵；来源：本地图鉴\n祝游戏愉快"}
    assert len(orchestrator.runs) == 1
    request = orchestrator.runs[0]
    assert request.scope.group_id == "1001"
    assert request.scope.user_id == "2001"
    assert request.scope.can_use_chat_memory is True
    assert request.deadline > datetime.now(UTC)
    assert request.route.allowed_tools == ("lookup_pet", "search_chat_memory")
    message = exc_info.value.message
    assert message.extract_plain_text() == "暗影格斗是洛克王国精灵；来源：本地图鉴\n祝游戏愉快"


@pytest.mark.asyncio
async def test_agent_path_clarification_replies_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """needs_clarification: direct stable reply, orchestrator never runs
    (S2-AGENT-09 + Scenario E)."""
    from qq_bot.agent.models import ReasonCode, RouteDecision, RouteKind
    from qq_bot.agent.router import RouteTrace

    class Store:
        async def add_message(self, **kwargs) -> int:
            return 1

    orchestrator = FakeOrchestrator(object())

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.4,
            reason_code=ReasonCode.CLARIFY,
            needs_clarification=True,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.4,
            reason_code=ReasonCode.CLARIFY,
            latency_ms=1.0,
            needs_clarification=True,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: Store())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 那个什么"))  # type: ignore[arg-type]

    assert orchestrator.runs == []  # no model, no orchestrator
    assert (
        str(exc_info.value.message)
        in (ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CLARIFY])
    )


@pytest.mark.asyncio
async def test_agent_path_capability_error_uses_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.agent.models import ReasonCode, RouteDecision, RouteKind
    from qq_bot.agent.router import RouteTrace

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.8,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            needs_clarification=True,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.8,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            latency_ms=1.0,
            needs_clarification=True,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    _patch_agent_runtime(
        monkeypatch, FakeAgentRuntime(FakeOrchestrator(object()), gateway=object())
    )
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 删除我的记忆"))  # type: ignore[arg-type]

    assert (
        str(exc_info.value.message)
        in (ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CAPABILITY_ERROR])
    )


@pytest.mark.asyncio
async def test_agent_path_safefailure_sends_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.agent.models import (
        FailureCode,
        ReasonCode,
        RouteDecision,
        RouteKind,
        SafeFailure,
    )
    from qq_bot.agent.router import RouteTrace

    class Store:
        async def add_message(self, **kwargs) -> int:
            return 1

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            latency_ms=1.0,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    failure = SafeFailure(code=FailureCode.DEADLINE_EXCEEDED, message="处理超时，请稍后重试。")
    orchestrator = FakeOrchestrator(failure)
    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: Store())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert str(exc_info.value.message) == "处理超时，请稍后重试。"
    assert len(orchestrator.runs) == 1


@pytest.mark.asyncio
async def test_agent_path_runtime_unavailable_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_runtime",
        lambda: (_ for _ in ()).throw(RuntimeStateError("not ready")),
    )
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert str(exc_info.value.message) in ai_chat_plugin._UNAVAILABLE_MESSAGES


@pytest.mark.asyncio
async def test_agent_flag_off_keeps_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_ENABLED=false: router and orchestrator are never touched; the
    legacy request_ai_reply path runs exactly as before (Task 13)."""

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        assert prompt == "你好"
        return "你好呀"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    async def unexpected_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        raise AssertionError("router must not run when AGENT_ENABLED=false")

    orchestrator = FakeOrchestrator(object())
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", unexpected_route_request)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert orchestrator.runs == []
    assert exc_info.value.message.extract_plain_text() == "你好呀"


@pytest.mark.asyncio
async def test_agent_path_skips_legacy_search_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With AGENT_ENABLED=true the legacy search call is skipped; routing
    owns search capability (S2-AGENT-09)."""
    from qq_bot.agent.models import (
        GroundedAnswer,
        ReasonCode,
        RouteDecision,
        RouteKind,
    )
    from qq_bot.agent.router import RouteTrace

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            latency_ms=1.0,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    async def unexpected_search(prompt: str, *, settings, client):
        raise AssertionError("legacy search must not run in agent mode")

    answer = GroundedAnswer(claims=())
    orchestrator = FakeOrchestrator(answer, store=object())
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: _agent_settings(search_enabled=True, tavily_api_key="tvly-test"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin, "search_web", unexpected_search)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 今天有什么新闻"))  # type: ignore[arg-type]

    assert str(exc_info.value.message) == ""


@pytest.mark.asyncio
async def test_ai_chat_quota_denial_blocks_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """S4-QUOTA-02: when the quota service denies admission the handler sends
    the stable denial text and never calls the model."""
    from qq_bot.services.chat_memory import ChatMemoryRepository
    from qq_bot.services.quota import QuotaService

    repository = ChatMemoryRepository(tmp_path / "quota.sqlite3", retention_days=30)
    await repository.open()
    try:
        service = QuotaService(
            BotSettings(
                allowed_group_ids="1001",
                ai_api_key="secret",
                quota_rate_limit_per_minute=1,
            ),
            repository,
        )
        # consume the single allowed slot for this scope
        assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed

        calls: list[str] = []

        async def unexpected_reply(**kwargs) -> str:
            calls.append("model")
            return "不应到达模型"

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(
            ai_chat_plugin,
            "get_settings",
            lambda: BotSettings(
                allowed_group_ids="1001",
                ai_api_key="secret",
                quota_rate_limit_per_minute=1,
            ),
        )
        monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
        _patch_agent_runtime(monkeypatch, _QuotaRuntime(service))
        monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", unexpected_reply)
        monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

        with pytest.raises(FinishCalled) as exc_info:
            await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

        assert calls == []
        assert str(exc_info.value.message).strip() == "提问太频繁了，请稍后再试。"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_ai_chat_quota_scope_binds_around_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """S4-QUOTA-03/04: while the model call runs, the quota scope is bound so
    usage accounting can attach the group without re-parsing the event."""
    from qq_bot.services.chat_memory import ChatMemoryRepository
    from qq_bot.services.quota import QuotaService, active_quota_scope

    repository = ChatMemoryRepository(tmp_path / "quota2.sqlite3", retention_days=30)
    await repository.open()
    try:
        service = QuotaService(
            BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
            repository,
        )
        seen: list[tuple[str, int] | None] = []

        async def spy_reply(prompt: str, **kwargs) -> str:
            seen.append(active_quota_scope())
            return "配额内回复"

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(
            ai_chat_plugin,
            "get_settings",
            lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
        )
        monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
        _patch_agent_runtime(monkeypatch, _QuotaRuntime(service))
        monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", spy_reply)
        monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

        with pytest.raises(FinishCalled) as exc_info:
            await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

        assert seen == [("group", 1001)]
        assert str(exc_info.value.message).strip() == "配额内回复"
    finally:
        await repository.close()


class _QuotaRuntime:
    def __init__(self, service: object):
        self._service = service

    def get_quota_service(self):
        return self._service


# ---- 自主群聊插话接线（S7-AUTO-07）----


@pytest.mark.asyncio
async def test_auto_chat_disabled_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"auto": 0}

    async def fake_run_auto_chat(**kwargs):
        calls["auto"] += 1

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=False),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert calls["auto"] == 0
    assert len(written) == 1
    assert written[0]["message_text"] == "随便聊聊"
    assert written[0]["is_ai_prompt"] is False


@pytest.mark.asyncio
async def test_auto_chat_enabled_routes_plain_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    await ai_chat_plugin.handle_ai_chat(FakeEvent("小洛在吗"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "小洛在吗"
    assert captured["settings"].auto_chat_enabled is True
    assert callable(captured["send"])
    assert callable(captured["quota_check"])
    assert captured["memory_store"] is not None


@pytest.mark.asyncio
async def test_auto_chat_skipped_when_memory_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"auto": 0}

    async def fake_run_auto_chat(**kwargs):
        calls["auto"] += 1

    def broken_repository():
        raise RuntimeError("no runtime")

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", broken_repository)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert calls["auto"] == 0


@pytest.mark.asyncio
async def test_auto_chat_quota_and_send_closures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send 闭包走 replace_named_mentions + finish；quota 闭包走 runtime。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            named_mention_replacements="@小洛=2880000001",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("来玩"))  # type: ignore[arg-type]

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    with pytest.raises(FinishCalled):
        await captured["send"]("你好，@小洛")


# ---- 固定文案池化（S7-AUTO-08）----


def test_clarify_and_unavailable_messages_are_pools() -> None:
    assert len(ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CLARIFY]) >= 3
    assert len(ai_chat_plugin._UNAVAILABLE_MESSAGES) >= 3
    for pool in ai_chat_plugin._AGENT_CLARIFY_MESSAGES.values():
        assert all(isinstance(m, str) and m for m in pool)


def test_pick_variants_stays_within_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ("a", "b", "c")
    monkeypatch.setattr(ai_chat_plugin.random, "choice", lambda seq: seq[0])
    assert ai_chat_plugin._pick_variants(pool) == "a"


# ---- 二期：被 @ 路径登记与冷场调度（S7-AUTO-P2-05）----


@pytest.mark.asyncio
async def test_addressed_reply_registers_note_and_cold_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"note": 0, "cold": 0}

    class FakeAutoChatState:
        def note_reply(self, group_id, *, settings):
            calls["note"] += 1
            return ""

    async def fake_request_ai_reply(
        prompt, *, settings, client=None, search_context="", chat_context="", roco_context=""
    ):
        return "你好呀"

    async def fake_finish(message):
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "shared_state", lambda: FakeAutoChatState())

    def fake_schedule_cold_check(**kwargs):
        calls["cold"] += 1

    monkeypatch.setattr(ai_chat_plugin, "schedule_cold_check", fake_schedule_cold_check)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert calls["note"] == 1
    assert calls["cold"] == 1


# ---- 二期收尾：send 闭包吞掉 finish 控制流异常（S7-AUTO-P2-07）----


@pytest.mark.asyncio
async def test_auto_chat_send_hook_swallows_finished_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matcher.finish 正常完成抛 FinishedException；send 闭包必须吞掉它，
    否则调用方的后续逻辑（如 run_auto_chat 的 send/ok 指标）永远不会执行。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, send = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    async def fake_finish(matcher, message):
        raise FinishedException()

    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    await send("x")  # 不抛出 FinishedException 即通过


# ---- 二期修复：多段连发（S7-AUTO-P2-10）----


@pytest.mark.asyncio
async def test_send_multi_parts_bursts_then_finishes_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOneBot:
        def __init__(self):
            self.sent: list[tuple[int, str]] = []

        async def send_group_msg(self, *, group_id, message):
            self.sent.append((group_id, str(message)))

    fake_bot = FakeOneBot()

    async def fake_finish(matcher, message):
        raise FinishCalled(message)

    event = FakeEvent("随便聊聊")
    settings = BotSettings(
        allowed_group_ids="1001",
        ai_api_key="secret",
        named_mention_replacements="@小洛=2880000001",
    )
    _, send = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: fake_bot)
    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.random, "uniform", lambda a, b: 0.0)

    with pytest.raises(FinishCalled) as exc_info:
        await send(["哈哈", "玩啊", "你好，@小洛"])

    # 前面的碎片走裸 API 连发；最后一段走 finish
    assert fake_bot.sent == [(1001, "哈哈"), (1001, "玩啊")]
    assert "你好，" in str(exc_info.value.message)
