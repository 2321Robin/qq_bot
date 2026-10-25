import asyncio
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from prometheus_client import REGISTRY

from qq_bot.config import BotSettings
from qq_bot.plugins import ai_chat as ai_chat_plugin
from qq_bot.runtime import RuntimeStateError
from qq_bot.services import quote_reference, roco_knowledge, vision_extract
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.sticker_library import Sticker
from pathlib import Path


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
        message_id: int = 1,
    ):
        self.text = text
        self.segments = segments or []
        self.to_me = to_me
        self.self_id = self_id
        self.message_id = message_id
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


@pytest.fixture(autouse=True)
def _fresh_shared_quote_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    """引用接线（S10-HUMAN-P0-06）使用进程级共享 LRU 单例；每个用例前重置，
    避免跨用例的消息登记残留影响引用解析结果。"""
    monkeypatch.setattr(quote_reference, "_SHARED_LRU", None)


@pytest.fixture(autouse=True)
def _fresh_shared_image_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨消息图片回忆（S11-VISION-01）同样走进程级单例；每例重置，避免
    前面用例 remember 的图片被后面用例 recall 到。"""
    monkeypatch.setattr(vision_extract, "_SHARED_IMAGE_RECALL", None)


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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "总结他的观点"
        assert "用户A：他的观点" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "继续"
        assert "用户A：之前的问题" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "刚才大家在说什么"
        assert "用户A：普通群友发言" in chat_context
        assert "用户B：另一个群友发言" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "总结他的观点"
        assert "用户A：他的观点" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "总结 最近三条消息"
        assert "用户A：目标用户的信息" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "你怎么看"
        assert "用户A：群聊观点" in chat_context
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
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
    """旁路直连路径（S10-HUMAN-P0 H1 后）：collect 关闭时普通消息仍直连管道。"""
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
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=False,
        ),
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
            auto_chat_collect_enabled=False,
            named_mention_replacements="@小洛=2880000001",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("来玩"))  # type: ignore[arg-type]

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    with pytest.raises(FinishCalled):
        await captured["send"]("你好，@小洛")


# ---- 收集窗口接线（S10-HUMAN-P0-04）----


@pytest.mark.asyncio
async def test_auto_chat_collect_route_stashes_hooks_and_submits_batch_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通消息不再直连 run_auto_chat：暂存 quota/send 钩子，携带
    row_id 的 BatchItem 交给收集器，并刷新 pending gauge。"""
    submitted: list[tuple[int, object]] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append((group_id, item))

        def pending_count(self) -> int:
            return len(submitted)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 777

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert len(submitted) == 1
    group_id, item = submitted[0]
    assert group_id == 1001
    assert item.raw_text == "随便聊聊"
    assert item.sender_user_id == 2001
    assert item.row_id == 777
    assert 1001 in ai_chat_plugin._PENDING_AUTO_HOOKS
    quota_check, send, react, send_sticker, trigger_images = ai_chat_plugin._PENDING_AUTO_HOOKS[
        1001
    ]
    assert callable(quota_check)
    assert callable(send)
    assert callable(react)
    assert callable(send_sticker)
    assert trigger_images == ()  # 普通文本消息无图，触发图占位为空元组


@pytest.mark.asyncio
async def test_auto_chat_collector_delivery_runs_pipeline_with_batch_and_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静默到期后 _deliver 整批交给 run_auto_chat(batch=...)：复用暂存钩子、
    触发文本取批次末条，交付后钩子弹出、pending 归零。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)
        await asyncio.sleep(0)  # 保持 _deliver 可等待的真实异步形态

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 888

    settings = BotSettings(
        allowed_group_ids="1001",
        auto_chat_enabled=True,
        auto_chat_collect_quiet_seconds=0.02,
        auto_chat_collect_max_seconds=0.5,
    )
    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(ai_chat_plugin, "_AUTO_CHAT_COLLECTOR", None)
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    for _ in range(100):
        if captured:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # 等 _run 任务完全收尾，避免悬空 task

    assert captured["raw_text"] == "随便聊聊"
    assert [it.raw_text for it in captured["batch"]] == ["随便聊聊"]
    assert captured["event"].group_id == 1001
    assert captured["settings"].auto_chat_collect_quiet_seconds == 0.02
    assert captured["memory_store"] is not None
    assert callable(captured["send"])
    assert callable(captured["quota_check"])
    assert 1001 not in ai_chat_plugin._PENDING_AUTO_HOOKS
    assert ai_chat_plugin._AUTO_CHAT_COLLECTOR.pending_count() == 0


def _auto_chat_metric(stage: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value("qq_bot_auto_chat_total", {"stage": stage, "result": result})
        or 0.0
    )


@pytest.mark.asyncio
async def test_auto_chat_bypass_direct_call_emits_collect_bypass_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点名旁路直连管道前补 collect/bypass 指标（spec 观测性要求）。"""
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
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=True,
            persona_name="小洛",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "bypass")
    await ai_chat_plugin.handle_ai_chat(FakeEvent("小洛在吗"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "小洛在吗"  # 旁路直连，未进收集窗口
    assert _auto_chat_metric("collect", "bypass") == before + 1


@pytest.mark.asyncio
async def test_auto_chat_collect_disabled_direct_call_emits_collect_disabled_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collect 关闭时直连管道前补 collect/disabled 指标（spec 观测性要求）。"""
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
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=False,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "disabled")
    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "随便聊聊"  # 直连管道，未进收集窗口
    assert _auto_chat_metric("collect", "disabled") == before + 1


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
    calls = {"note": 0, "cold": 0, "activity": []}

    class FakeAutoChatState:
        def note_reply(self, group_id, *, settings):
            calls["note"] += 1
            return ""

        def note_activity(self, group_id, *, weight=1.0, boost=None, half_life=None):
            calls["activity"].append((group_id, weight, boost, half_life))

    async def fake_request_ai_reply(
        prompt,
        *,
        settings,
        client=None,
        search_context="",
        chat_context="",
        roco_context="",
        images=(),
        extra_system_prompt="",
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
    # 群兴趣度计分（H7）：被 @ 问答路径同入口记一次，weight 由纯函数算出，
    # boost 显式取自 settings.auto_chat_interest_boost
    assert len(calls["activity"]) == 1
    group_id, weight, boost, half_life = calls["activity"][0]
    assert group_id == 1001
    assert weight == 3.0  # "ai 你好" 含"你"（+2），非疑问尾
    assert boost == BotSettings().auto_chat_interest_boost
    # 存量折叠接线（Final Fix）：handler 显式传半衰期配置，note 前先折算
    assert half_life == BotSettings().auto_chat_interest_half_life


# ---- 二期收尾：send 闭包吞掉 finish 控制流异常（S7-AUTO-P2-07）----


@pytest.mark.asyncio
async def test_auto_chat_send_hook_swallows_finished_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matcher.finish 正常完成抛 FinishedException；send 闭包必须吞掉它，
    否则调用方的后续逻辑（如 run_auto_chat 的 send/ok 指标）永远不会执行。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, send, _react, _sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    async def fake_finish(matcher, message):
        raise FinishedException()

    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    await send("x")  # 不抛出 FinishedException 即通过


# ---- 贴表情 react 闭包（S10-HUMAN-P2 H9，Fix Round 1 / Minor 5）----


@pytest.mark.asyncio
async def test_auto_chat_react_hook_returns_false_without_connected_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无已连接 OneBot v11 bot：闭包必须返回 False（由 auto_chat 服务层
    的连续失败降级逻辑处置），绝不抛异常。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    assert await react(42, "128") is False


@pytest.mark.asyncio
async def test_auto_chat_react_hook_calls_set_msg_emoji_like_with_protocol_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议契约钉死：成功路径调用 `call_api("set_msg_emoji_like",
    message_id=<int>, emoji_id=<str>)` 并返回 True（NapCat 参数名/类型）。"""
    recorded: list[tuple[str, dict]] = []

    class FakeOneBot:
        async def call_api(self, action: str, **params):
            recorded.append((action, params))
            return None  # 协议成功时返回值无关紧要

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeOneBot())

    assert await react(42, "128") is True
    assert recorded == [("set_msg_emoji_like", {"message_id": 42, "emoji_id": "128"})]


@pytest.mark.asyncio
async def test_auto_chat_react_hook_swallows_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议不支持/超时/断连：call_api 抛异常时闭包返回 False，不向上抛。"""

    class ExplodingOneBot:
        async def call_api(self, action: str, **params):
            raise RuntimeError("protocol unsupported")

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: ExplodingOneBot())

    assert await react(42, "128") is False


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
    _, send, _react, _sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: fake_bot)
    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.random, "uniform", lambda a, b: 0.0)

    with pytest.raises(FinishCalled) as exc_info:
        await send(["哈哈", "玩啊", "你好，@小洛"])

    # 前面的碎片走裸 API 连发；最后一段走 finish
    assert fake_bot.sent == [(1001, "哈哈"), (1001, "玩啊")]
    assert "你好，" in str(exc_info.value.message)


# ---- 引用接线（S10-HUMAN-P0-06）：事件层渲染 + quoted 必回 ----


class FakeReplySegment:
    type = "reply"

    def __init__(self, data: dict):
        self.data = data


@pytest.mark.asyncio
async def test_quoted_self_message_bypasses_collector_and_replies_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用机器人自己的消息（H4 quoted 必回）：设 quoted_self 旁路直连管道，
    不进收集窗口，也不渲染引用标记（存储与管道输入都不带 u{id}）。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 321

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    def _no_collector():
        raise AssertionError("quoted-self message must bypass the collector")

    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", _no_collector)
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "bypass")
    event = FakeEvent(
        "随便聊聊",
        segments=[FakeReplySegment({"id": 7, "qq": 2880000001, "text": "机器人之前的话"})],
    )
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert captured["raw_text"] == "随便聊聊"  # self 引用不渲染标记
    assert _auto_chat_metric("collect", "bypass") == before + 1
    assert written[0]["message_text"] == "随便聊聊"  # 存储同样无标记


@pytest.mark.asyncio
async def test_quoted_other_user_renders_marker_into_pipeline_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用他人（H4 事件层渲染）：reply 段扩展字段解析后渲染 u{id} 标记进
    原始文本（只进本地存储/管道输入），且不触发 quoted 旁路。"""
    submitted: list[object] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append(item)

        def pending_count(self) -> int:
            return len(submitted)

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 654

    auto_calls: list = []

    async def fake_run_auto_chat(**kwargs):
        auto_calls.append(kwargs)

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    event = FakeEvent("冲", segments=[FakeReplySegment({"id": 8, "qq": 12345, "text": "上分"})])
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert len(submitted) == 1
    assert submitted[0].raw_text == "回复 [引用 u12345:上分]：冲"
    assert auto_calls == []  # 普通引用不走旁路
    # Fix Round 1：存储侧同样带标记（非 self 方向的入库文本可回查引用上下文）
    assert written[0]["message_text"] == "回复 [引用 u12345:上分]：冲"


@pytest.mark.asyncio
async def test_quote_reply_disabled_ignores_reply_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：reply 段被完全忽略，引用自己也不旁路（接线前行为）。"""
    submitted: list[object] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append(item)

        def pending_count(self) -> int:
            return len(submitted)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 999

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_quote_reply_enabled=False,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    event = FakeEvent(
        "随便聊聊",
        segments=[FakeReplySegment({"id": 9, "qq": 2880000001, "text": "机器人之前的话"})],
    )
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert len(submitted) == 1
    assert submitted[0].raw_text == "随便聊聊"  # 未渲染、未旁路


# ---- Fix Round 1（S10-HUMAN-P0-06）：标记只进存储/auto 管道，命令/提问
# ---- 提取继续用未加标记的 raw_text ----


@pytest.mark.asyncio
async def test_quoted_reply_with_ai_prefix_still_explicit_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix Round 1 回归 A：reply + "ai xxx" 前缀仍识别为显式 AI 命令；
    提示词不含 u{id} 令牌与 [引用 标记（标记只进存储）。"""
    captured: dict = {}

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        captured["prompt"] = prompt
        return "帮你看了"

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 555

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            auto_chat_enabled=False,
            auto_chat_quote_reply_enabled=True,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    event = FakeEvent(
        "ai 帮我看看这个",
        segments=[FakeReplySegment({"id": 11, "qq": 12345, "text": "上分"})],
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    prompt = captured["prompt"]
    assert prompt == "帮我看看这个"
    assert "u12345" not in prompt
    assert "[引用" not in prompt
    # 存储侧仍带标记：引用上下文保留给本地存储与后续渲染
    assert written[0]["message_text"] == "回复 [引用 u12345:上分]：ai 帮我看看这个"


@pytest.mark.asyncio
async def test_quoted_reply_addressed_to_bot_prompt_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix Round 1 回归 B：reply + @bot（to_me）走显式应答路径；提示词绝不
    携带原始 u{id} 令牌（QQ 号经模型回显进群聊即隐私边界破防）。"""
    captured: dict = {}

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        captured["prompt"] = prompt
        return "看到了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            auto_chat_quote_reply_enabled=True,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    event = FakeEvent(
        "帮我看看这个",
        segments=[FakeReplySegment({"id": 12, "qq": 12345, "text": "上分"})],
        to_me=True,
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    prompt = captured["prompt"]
    assert prompt == "帮我看看这个"
    assert "u12345" not in prompt
    assert "[引用" not in prompt


# ---------------------------------------------------------------------------
# 表情包发送钩子（S10-HUMAN-P2 H10）
# ---------------------------------------------------------------------------


def _sticker() -> Sticker:
    return Sticker(id="cat_laugh", path=Path("/tmp/cat_laugh.png"), description="猫大笑")


@pytest.mark.asyncio
async def test_send_sticker_hook_returns_false_without_connected_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无已连接 bot：闭包返回 False（服务层连续失败降级逻辑处置）。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    assert await send_sticker(_sticker()) is False


@pytest.mark.asyncio
async def test_send_sticker_hook_sends_image_segment_to_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功路径：库条目的本地绝对路径作为 image 段发到本群（与宠物卡同一
    `file:///` 惯例），返回 True。"""
    recorded: dict = {}

    class FakeOneBot:
        async def send_group_msg(self, *, group_id: int, message):
            recorded["group_id"] = group_id
            recorded["message"] = message
            return None

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeOneBot())

    assert await send_sticker(_sticker()) is True
    assert recorded["group_id"] == 1001
    segment = recorded["message"]
    assert segment.type == "image"
    assert segment.data["file"].startswith("file:///")


@pytest.mark.asyncio
async def test_send_sticker_hook_swallows_send_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议拒绝/文件不可读：send_group_msg 抛异常时闭包返回 False，不向上抛。"""

    class ExplodingOneBot:
        async def send_group_msg(self, *, group_id: int, message):
            raise RuntimeError("protocol unsupported")

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: ExplodingOneBot())

    assert await send_sticker(_sticker()) is False


# ---- 联网判别门（S11-SEARCH-01）：关键词未命中时由模型判断是否需要搜 ----


@pytest.mark.asyncio
async def test_ai_chat_search_gate_searches_with_model_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.search import SearchResult

    gate_calls: list[dict] = []

    async def fake_request_completion(**kwargs):
        gate_calls.append(kwargs)
        return '{"search": true, "query": "Kimi k3.1pro 发布"}'

    async def fake_search_web(prompt: str, *, settings: BotSettings, client: object | None = None):
        assert prompt == "Kimi k3.1pro 发布"  # 用判别门产出的搜索词，而非原话
        return [SearchResult("K3.1Pro 发布", "https://example.com/k3", "k3 info")]

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert prompt == "评价一下kimi k3.1pro"
        assert "K3.1Pro 发布" in search_context
        return "查到了。"

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
            ai_search_gate_enabled=True,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_completion", fake_request_completion)
    monkeypatch.setattr(ai_chat_plugin, "search_web", fake_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 评价一下kimi k3.1pro"))  # type: ignore[arg-type]

    assert gate_calls and gate_calls[0]["max_tokens"] == 128
    assert gate_calls[0]["json_mode"] is True


@pytest.mark.asyncio
async def test_ai_chat_search_gate_skips_when_model_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_completion(**kwargs):
        return '{"search": false, "query": ""}'

    async def unexpected_search_web(*args, **kwargs):
        raise AssertionError("search should not be called when gate says no")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert search_context == ""
        return "随便聊聊。"

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
            ai_search_gate_enabled=True,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_completion", fake_request_completion)
    monkeypatch.setattr(ai_chat_plugin, "search_web", unexpected_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 评价一下kimi k3.1pro"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_search_gate_fail_open_on_gate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_completion(**kwargs):
        raise RuntimeError("gate model down")

    async def unexpected_search_web(*args, **kwargs):
        raise AssertionError("search should not be called when gate errors")

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
        images: tuple[str, ...] = (),
        extra_system_prompt: str = "",
    ) -> str:
        assert search_context == ""
        return "这个本鸡真不知道。"

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            search_enabled=True,
            tavily_api_key="tvly-secret",
            ai_search_gate_enabled=True,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_completion", fake_request_completion)
    monkeypatch.setattr(ai_chat_plugin, "search_web", unexpected_search_web)
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 评价一下kimi k3.1pro"))  # type: ignore[arg-type]


# ---- 视觉（S11-VISION-01）：@问答读图 / 回复发图发表情 ----


class FakeImageSegment:
    type = "image"

    def __init__(self, url: str):
        self.data = {"url": url}


def _sticker_library(tmp_path: Path):
    from qq_bot.services.sticker_library import StickerLibrary

    sticker_path = tmp_path / "dog.png"
    sticker_path.write_bytes(b"png")
    return StickerLibrary(stickers=(Sticker(id="dog", path=sticker_path, description="一只狗"),))


@pytest.mark.asyncio
async def test_ai_chat_passes_images_and_attachment_placeholder_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@机器人 + 只发一张图：占位文案进 prompt，图片 URL 随请求发出，
    记忆行保留 [图片] 占位。"""
    captured: dict = {}
    stored: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            stored.append(kwargs)
            return 55

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured.update(kwargs)
        captured["prompt"] = prompt
        return "这是张梗图"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    event = FakeEvent(
        "",
        [FakeImageSegment("https://multimedia.example/meme.jpg")],
        to_me=True,
    )
    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert captured["prompt"] == "[图片]"
    assert captured["images"] == ("https://multimedia.example/meme.jpg",)
    assert stored and stored[0]["message_text"].endswith("[图片]")
    assert exc_info.value.message.extract_plain_text() == "这是张梗图"


@pytest.mark.asyncio
async def test_ai_chat_face_segment_becomes_tag_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QQ 经典表情渲染成 [表情:名字] 占位随问题进模型。"""
    captured: dict = {}

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["prompt"] = prompt
        captured["images"] = kwargs.get("images")
        return "哈哈"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    face_segment = SimpleNamespace(type="face", data={"id": "21", "text": "白眼"})
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent("这什么意思", [FakeTextSegment("这什么意思"), face_segment], to_me=True)
        )

    assert captured["prompt"] == "这什么意思 [表情:白眼]"
    assert captured["images"] == ()


def test_build_outgoing_reply_plain_text_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = BotSettings()
    monkeypatch.setattr(ai_chat_plugin, "_deliver_sticker_library", lambda: None)
    message = ai_chat_plugin._build_outgoing_reply("就一句话", settings=settings)
    assert isinstance(message, Message)
    assert message.extract_plain_text() == "就一句话"


def test_build_outgoing_reply_sticker_marker_becomes_image_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:dog]", settings=settings)

    assert isinstance(message, Message)
    assert [seg.type for seg in message] == ["text", "image"]
    assert message[0].data["text"] == "哈哈"
    assert message[1].data["file"].startswith("file:///")
    assert message[1].data["file"].endswith("dog.png")


def test_build_outgoing_reply_face_marker_becomes_face_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("[发脸:呲牙]没了", settings=settings)

    assert isinstance(message, Message)
    assert [seg.type for seg in message] == ["face", "text"]
    assert message[0].data["id"] == "13"
    assert message[1].data["text"] == "没了"


def test_build_outgoing_reply_unknown_markers_are_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """库缺条目/白名单外表情名：标记整段丢弃，绝不出现在群消息里。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:cat][发脸:菜刀]", settings=settings)

    assert message.extract_plain_text() == "哈哈"
    assert all(seg.type != "image" and seg.type != "face" for seg in message)


def test_build_outgoing_reply_disabled_channel_still_strips_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """开关关掉后模型漏出的标记也不许原文进群（剥掉，只发文本）。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=False)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:dog]", settings=settings)

    assert message.extract_plain_text() == "哈哈"
    assert all(seg.type != "image" for seg in message)


def test_qa_sticker_hint_face_channel_survives_empty_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QQ 小表情不依赖表情包库：库为空时 [发脸] 提示必须在，表情包候选块缺席
    （2026-10-25 线上教训：服务器没建库，整块提示被吞，模型发不了表情）。"""
    monkeypatch.setattr(ai_chat_plugin, "_deliver_sticker_library", lambda: None)
    hint = ai_chat_plugin._qa_sticker_hint(BotSettings(ai_qa_sticker_enabled=True))
    assert "[发脸:名字]" in hint
    assert "呲牙" in hint
    assert "表情包候选" not in hint
    assert "[发图:ID]" not in hint


def test_qa_sticker_hint_lists_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    hint = ai_chat_plugin._qa_sticker_hint(BotSettings(ai_qa_sticker_enabled=True))
    assert "[发图:dog]" in hint or "发图" in hint
    assert "一只狗" in hint
    assert "呲牙" in hint


# ---- 跨消息图片回忆接线（S11-VISION-01 补丁）----


@pytest.mark.asyncio
async def test_ai_chat_recalls_recent_group_image_when_at_message_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表情包先发一条、@机器人再发一条（本条无图）：回捞最近一张随请求发出。"""
    from qq_bot.services.vision_extract import ImageRecallBuffer

    captured: dict = {}
    recall_buffer = ImageRecallBuffer()

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["prompt"] = prompt
        captured["images"] = kwargs.get("images")
        return "看到了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "shared_image_recall", lambda: recall_buffer)

    # 第一条：普通带图消息（非 @）
    await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
        FakeEvent("", [FakeImageSegment("https://multimedia.example/meme.jpg")])
    )
    # 第二条：@机器人纯文本
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "看得到吗", [FakeAtSegment(2880000001), FakeTextSegment("看得到吗")], to_me=True
            )
        )

    assert captured["images"] == ("https://multimedia.example/meme.jpg",)
    assert "已附在本条消息里" in captured["prompt"]


@pytest.mark.asyncio
async def test_ai_chat_recall_disabled_keeps_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RECALL_SECONDS=0：不回捞，行为与纯文本版一致。"""
    from qq_bot.services.vision_extract import ImageRecallBuffer

    captured: dict = {}
    recall_buffer = ImageRecallBuffer()

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["images"] = kwargs.get("images")
        return "看不到"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001", ai_api_key="secret", ai_qa_vision_recall_seconds=0
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "shared_image_recall", lambda: recall_buffer)

    await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
        FakeEvent("", [FakeImageSegment("https://multimedia.example/meme.jpg")])
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "看得到吗", [FakeAtSegment(2880000001), FakeTextSegment("看得到吗")], to_me=True
            )
        )

    assert captured["images"] == ()


@pytest.mark.asyncio
async def test_ai_chat_own_image_wins_over_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@消息自带图时用本条的图，不回捞旧图。"""
    from qq_bot.services.vision_extract import ImageRecallBuffer

    captured: dict = {}
    recall_buffer = ImageRecallBuffer()
    recall_buffer.remember(1001, ["https://multimedia.example/old.jpg"], sender_user_id=2002)

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["images"] = kwargs.get("images")
        return "新图"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "shared_image_recall", lambda: recall_buffer)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "看这张",
                [
                    FakeAtSegment(2880000001),
                    FakeTextSegment("看这张"),
                    FakeImageSegment("https://multimedia.example/fresh.jpg"),
                ],
                to_me=True,
            )
        )

    assert captured["images"] == ("https://multimedia.example/fresh.jpg",)


# ---- 回捞图归属（S11-VISION-01：模型看得到图但不知道是谁发的）----


@pytest.mark.asyncio
async def test_recalled_image_note_attributes_other_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别人发的图 + 我来提问：注脚用上下文一致的"用户X"标注，并点明不是提问者发的。"""
    from qq_bot.services.vision_extract import RecalledImage

    note = ai_chat_plugin._recalled_image_note(
        RecalledImage(urls=("https://img.example/a.jpg",), sender_user_id=2002),
        SimpleNamespace(user_id=2001, group_id=1001),
        context_rows=[],
    )
    assert "用户A" in note
    assert "不是当前提问的用户发的" in note

    # 发送者已在上下文行里时，别名与上下文渲染一致
    row = ChatMemoryRow(
        id=1,
        group_id=1001,
        user_id=2002,
        message_text="[图片]",
        created_at="",
        is_ai_prompt=False,
        ai_reply="",
    )
    note2 = ai_chat_plugin._recalled_image_note(
        RecalledImage(urls=("https://img.example/a.jpg",), sender_user_id=2002),
        SimpleNamespace(user_id=2001, group_id=1001),
        context_rows=[row],
    )
    assert "用户A" in note2  # 2002 在上下文里排第一个 → 用户A


@pytest.mark.asyncio
async def test_recalled_image_note_attributes_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.services.vision_extract import RecalledImage

    note = ai_chat_plugin._recalled_image_note(
        RecalledImage(urls=("https://img.example/a.jpg",), sender_user_id=2001),
        SimpleNamespace(user_id=2001, group_id=1001),
        context_rows=[],
    )
    assert "当前提问的用户自己刚发的" in note


@pytest.mark.asyncio
async def test_recall_note_reaches_model_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：B 发图 → A @提问，注脚（含归属）进 prompt，图片随请求发出。"""
    from qq_bot.services.vision_extract import ImageRecallBuffer

    captured: dict = {}
    recall_buffer = ImageRecallBuffer()

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["prompt"] = prompt
        captured["images"] = kwargs.get("images")
        return "看到了"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "shared_image_recall", lambda: recall_buffer)

    # B（2002）发图
    await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
        FakeEvent(
            "",
            [FakeImageSegment("https://multimedia.example/meme.jpg")],
            user_id=2002,
        )
    )
    # A（2001）@提问
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "看得到吗",
                [FakeAtSegment(2880000001), FakeTextSegment("看得到吗")],
                to_me=True,
                user_id=2001,
            )
        )

    assert captured["images"] == ("https://multimedia.example/meme.jpg",)
    assert "不是当前提问的用户发的" in captured["prompt"]


@pytest.mark.asyncio
async def test_recalled_image_note_uses_group_nickname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """昵称解析接通后，回捞注脚用真实群昵称而非"用户A/B"字母。"""
    from qq_bot.services.vision_extract import RecalledImage

    note = ai_chat_plugin._recalled_image_note(
        RecalledImage(urls=("https://img.example/a.jpg",), sender_user_id=2002),
        SimpleNamespace(user_id=2001, group_id=1001),
        context_rows=[],
        nicknames={2002: "阿伟"},
    )
    assert "阿伟" in note
    assert "用户" not in note.split("不是当前提问的用户发的")[0]


@pytest.mark.asyncio
async def test_qa_chat_context_resolves_nicknames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：AUTO_CHAT_NICKNAMES_ENABLED 开启时，@问答上下文按昵称渲染。"""
    captured: dict = {}

    async def fake_request_ai_reply(prompt, **kwargs) -> str:
        captured["chat_context"] = kwargs.get("chat_context")
        return "好"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    class NicknameStore(EmptyMemoryStore):
        async def recent_group_messages(self, *, group_id: int, limit: int):
            return [
                ChatMemoryRow(
                    id=1,
                    group_id=group_id,
                    user_id=2002,
                    message_text="哈哈",
                    created_at="",
                    is_ai_prompt=False,
                    ai_reply="",
                )
            ]

    async def fake_resolver(user_ids):
        return {2002: "阿伟"}

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001", ai_api_key="secret", auto_chat_nicknames_enabled=True
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: NicknameStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(
        ai_chat_plugin, "_nickname_resolver", lambda group_id, settings: fake_resolver
    )

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(  # type: ignore[arg-type]
            FakeEvent(
                "看得到吗", [FakeAtSegment(2880000001), FakeTextSegment("看得到吗")], to_me=True
            )
        )

    assert "阿伟：哈哈" in captured["chat_context"]


def test_build_outgoing_reply_raw_face_id_resend_client_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[发脸:#编号]：客户端认识的 id 可以原样回发（读侧占位自带 id）。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("给[发脸:#352]", settings=settings)

    assert isinstance(message, Message)
    assert [seg.type for seg in message] == ["text", "face"]
    assert message[1].data["id"] == "352"


def test_build_outgoing_reply_unknown_face_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """客户端不认识的 id（493>417）在解析层就拦下，段不会进消息。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("给[发脸:#493]", settings=settings)

    assert message.extract_plain_text() == "给"
    assert all(seg.type != "face" for seg in message)


def test_qa_sticker_hint_does_not_promise_unknown_faces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """提示词不教回发未知表情（发出去只会被客户端丢弃，制造"我说发了
    你没看到"），并明确告知发不出来。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    hint = ai_chat_plugin._qa_sticker_hint(BotSettings(ai_qa_sticker_enabled=True))
    assert "[发脸:#编号]" not in hint
    assert "发不出来" in hint
