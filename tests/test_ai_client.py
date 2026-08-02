import httpx
from datetime import datetime
import pytest

from qq_bot.config import BotSettings
from qq_bot.runtime import BREAKER_AI_PRIMARY
from qq_bot.services import ai_client
from qq_bot.services.ai_client import AIReplyError, build_chat_payload, request_ai_reply
from qq_bot.services.reliability import TRANSIENT, CircuitBreaker


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request, headers=self.headers),
            )

    def json(self) -> dict:
        return self.payload


class InvalidJsonResponse(FakeResponse):
    def json(self) -> dict:
        raise ValueError("not json")


class HttpErrorResponse(FakeResponse):
    """Server-side failure (5xx): transient, retried before fallback."""

    def __init__(self, payload: dict, status_code: int = 500):
        super().__init__(payload, status_code=status_code)


class SequenceClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict] = []

    async def post(
        self, url: str, *, headers: dict, json: dict, timeout: float | None = None
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class FallbackAccessSettings(BotSettings):
    @property
    def normalized_ai_fallback_base_url(self) -> str:
        raise AssertionError("fallback should not be attempted")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def post(
        self, url: str, *, headers: dict, json: dict, timeout: float | None = None
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _fast_retry_settings(**overrides) -> BotSettings:
    """Retry tests must not actually sleep: near-zero delays, no jitter."""
    defaults = {
        "ai_max_attempts": 2,
        "ai_retry_base_delay_seconds": 0.001,
        "ai_retry_max_delay_seconds": 0.002,
        "retry_jitter_ratio": 0.0,
    }
    defaults.update(overrides)
    return BotSettings(**defaults)


def test_build_chat_payload_uses_model_and_prompt() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("你好", settings)

    assert payload["model"] == "test-model"
    assert payload["messages"][-1] == {"role": "user", "content": "你好"}


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


def test_build_chat_payload_limits_reply_length() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("今天新闻", settings)

    assert payload["max_tokens"] == 600
    assert "控制在 600 字以内" in payload["messages"][0]["content"]


def test_build_chat_payload_uses_natural_group_chat_style() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("今天新闻", settings)

    system_prompt = payload["messages"][0]["content"]
    assert "像 QQ 群友聊天" in system_prompt
    assert "直接回答" in system_prompt
    assert "不要总用“好的”" in system_prompt


def test_build_chat_payload_requires_source_section_with_search_context() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "今天新闻",
        settings,
        search_context="[1] Example\nURL: https://example.com\nSummary: summary",
    )

    system_prompt = payload["messages"][0]["content"]
    assert "末尾加“来源：”" in system_prompt
    assert "最多 3 条" in system_prompt
    assert "不要编造链接" in system_prompt


def test_build_chat_payload_injects_current_local_time() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "今天几号",
        settings,
        current_time="2026-05-09 19:30",
    )

    system_prompt = payload["messages"][0]["content"]
    assert "当前本地时间：2026-05-09 19:30" in system_prompt


def test_format_current_local_time_includes_matching_weekday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls) -> datetime:
            return datetime(2026, 6, 6, 21, 30)

    monkeypatch.setattr(ai_client, "datetime", FixedDatetime)

    assert ai_client._format_current_local_time() == "2026-06-06 21:30，星期六"


def test_build_chat_payload_tells_model_not_to_recalculate_current_time() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "今天星期几",
        settings,
        current_time="2026-06-06 21:30，星期六",
    )

    system_prompt = payload["messages"][0]["content"]
    assert "当前本地时间：2026-06-06 21:30，星期六" in system_prompt
    assert "回答当前日期、时间、星期时必须以该本地时间为准" in system_prompt


def test_build_chat_payload_uses_reliability_rules_with_search_context() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "BTC 价格",
        settings,
        current_time="2026-05-09 19:30",
        search_context="[1] Price\nURL: https://example.com\nSummary: summary",
    )

    system_prompt = payload["messages"][0]["content"]
    assert "不要编造事实" in system_prompt
    assert "不要编造链接" in system_prompt
    assert "不要编造时间" in system_prompt
    assert "不要编造价格" in system_prompt
    assert "没有可靠来源" in system_prompt


def test_build_chat_payload_includes_chat_context_when_provided() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "继续刚才的话题",
        settings,
        chat_context="历史聊天记录：\n用户2001：ai 你好\n机器人：你好呀",
    )

    user_message = payload["messages"][-1]["content"]
    system_prompt = payload["messages"][0]["content"]
    assert user_message == (
        "当前用户问题：继续刚才的话题\n\n历史聊天记录：\n用户2001：ai 你好\n机器人：你好呀"
    )
    assert user_message.count("历史聊天记录") == 1
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
    assert user_message == (
        "当前用户问题：这事现在怎么样\n\n"
        "历史聊天记录：\n"
        "用户2001：之前说过 DeepSeek\n\n"
        "联网搜索资料：\n"
        "[1] News\n"
        "URL: https://example.com\n"
        "摘要: summary"
    )
    assert user_message.count("历史聊天记录") == 1


def test_build_chat_payload_includes_roco_context_when_provided() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "画精灵怎么进化？",
        settings,
        roco_context="问题类型：进化\n匹配精灵：画精灵\n后续进化：画像守护",
    )

    user_message = payload["messages"][-1]["content"]
    system_prompt = payload["messages"][0]["content"]
    assert "本地洛克王国资料" in user_message
    assert "匹配精灵：画精灵" in user_message
    assert "可信的本地数据" in system_prompt
    assert "本地数据没有记录" in system_prompt


def test_build_chat_payload_combines_roco_with_other_contexts() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "这只精灵今天还有新闻吗",
        settings,
        search_context="[1] News\nURL: https://example.com\n摘要: summary",
        chat_context="历史聊天记录：\n用户2001：刚才说画精灵",
        roco_context="问题类型：精灵资料\n匹配精灵：画精灵",
    )

    user_message = payload["messages"][-1]["content"]
    assert user_message == (
        "当前用户问题：这只精灵今天还有新闻吗\n\n"
        "历史聊天记录：\n"
        "用户2001：刚才说画精灵\n\n"
        "本地洛克王国资料：\n"
        "问题类型：精灵资料\n"
        "匹配精灵：画精灵\n\n"
        "联网搜索资料：\n"
        "[1] News\n"
        "URL: https://example.com\n"
        "摘要: summary"
    )


@pytest.mark.asyncio
async def test_request_ai_reply_posts_openai_compatible_payload() -> None:
    settings = BotSettings(
        ai_api_key="secret",
        ai_base_url="https://api.example.com/v1/",
        ai_model="test-model",
    )
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "机器人回复"}}]}))

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "机器人回复"
    assert client.calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert client.calls[0]["json"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_request_ai_reply_does_not_call_fallback_when_primary_succeeds() -> None:
    settings = BotSettings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient([FakeResponse({"choices": [{"message": {"content": "主服务回复"}}]})])

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "主服务回复"
    assert len(client.calls) == 1
    assert client.calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert client.calls[0]["json"]["model"] == "primary-model"


@pytest.mark.asyncio
async def test_request_ai_reply_uses_fallback_when_primary_fails() -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1/",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient(
        [
            HttpErrorResponse({}),
            HttpErrorResponse({}),
            FakeResponse({"choices": [{"message": {"content": "备用服务回复"}}]}),
        ]
    )

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "备用服务回复"
    # primary exhausts its 2 attempts, then the fallback succeeds once
    assert len(client.calls) == 3
    assert client.calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer primary-secret"
    assert client.calls[0]["json"]["model"] == "primary-model"
    assert client.calls[1]["url"] == "https://primary.example.com/v1/chat/completions"
    assert client.calls[2]["url"] == "https://fallback.example.com/v1/chat/completions"
    assert client.calls[2]["headers"]["Authorization"] == "Bearer fallback-secret"
    assert client.calls[2]["json"]["model"] == "fallback-model"


@pytest.mark.asyncio
async def test_request_ai_reply_preserves_failure_when_fallback_is_not_configured() -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="",
    )
    client = SequenceClient([HttpErrorResponse({}), HttpErrorResponse({})])

    with pytest.raises(AIReplyError, match="AI API request failed"):
        await request_ai_reply("你好", settings=settings, client=client)

    assert len(client.calls) == 2  # both attempts exhausted


@pytest.mark.asyncio
async def test_request_ai_reply_retries_transient_failure_then_succeeds() -> None:
    settings = _fast_retry_settings(
        ai_api_key="secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_max_attempts=3,
    )
    client = SequenceClient(
        [
            HttpErrorResponse({}),
            HttpErrorResponse({}),
            FakeResponse({"choices": [{"message": {"content": "重试后成功"}}]}),
        ]
    )

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "重试后成功"
    assert len(client.calls) == 3
    assert all(
        call["url"] == "https://primary.example.com/v1/chat/completions" for call in client.calls
    )


@pytest.mark.asyncio
async def test_request_ai_reply_does_not_retry_401_and_falls_back_once() -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient(
        [
            HttpErrorResponse({}, status_code=401),
            FakeResponse({"choices": [{"message": {"content": "备用回复"}}]}),
        ]
    )

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "备用回复"
    # 401 is permanent: exactly one primary call, no retry
    assert len(client.calls) == 2
    assert client.calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert client.calls[1]["url"] == "https://fallback.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_request_ai_reply_both_providers_exhausted_raises() -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient([HttpErrorResponse({}) for _ in range(4)])

    with pytest.raises(AIReplyError, match="AI API request failed"):
        await request_ai_reply("你好", settings=settings, client=client)

    assert len(client.calls) == 4  # 2 primary + 2 fallback attempts


@pytest.mark.asyncio
async def test_request_ai_reply_primary_circuit_open_skips_primary_and_falls_back(
    monkeypatch,
) -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )

    open_breaker = CircuitBreaker(name="ai_primary", failure_threshold=1, recovery_seconds=30)
    await open_breaker.on_failure(TRANSIENT)  # open now

    def fake_breaker_for(name: str):
        if name == BREAKER_AI_PRIMARY:
            return open_breaker
        return CircuitBreaker(name=name, failure_threshold=3, recovery_seconds=30)

    monkeypatch.setattr(ai_client, "_breaker_for", fake_breaker_for)
    client = SequenceClient([FakeResponse({"choices": [{"message": {"content": "备用回复"}}]})])

    reply = await request_ai_reply("你好", settings=settings, client=client)

    assert reply == "备用回复"
    assert len(client.calls) == 1
    assert client.calls[0]["url"] == "https://fallback.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_request_ai_reply_invalid_response_is_not_retried() -> None:
    settings = _fast_retry_settings(
        ai_api_key="secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_max_attempts=3,
    )
    client = FakeClient(InvalidJsonResponse({}))

    with pytest.raises(AIReplyError, match="invalid response"):
        await request_ai_reply("你好", settings=settings, client=client)

    assert len(client.calls) == 1  # parsing failures are permanent


@pytest.mark.asyncio
async def test_request_ai_reply_empty_content_is_not_retried() -> None:
    settings = _fast_retry_settings(
        ai_api_key="secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_max_attempts=3,
    )
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "   "}}]}))

    with pytest.raises(AIReplyError, match="empty response"):
        await request_ai_reply("你好", settings=settings, client=client)

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_request_ai_reply_empty_prompt_does_not_call_fallback() -> None:
    settings = FallbackAccessSettings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient([])

    with pytest.raises(AIReplyError, match="prompt cannot be empty"):
        await request_ai_reply("  ", settings=settings, client=client)

    assert len(client.calls) == 0


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


@pytest.mark.asyncio
async def test_request_ai_reply_posts_roco_context_payload() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "本地资料回复"}}]}))

    reply = await request_ai_reply(
        "画精灵怎么进化？",
        settings=settings,
        client=client,
        roco_context="问题类型：进化\n匹配精灵：画精灵",
    )

    assert reply == "本地资料回复"
    user_message = client.calls[0]["json"]["messages"][-1]["content"]
    assert "画精灵怎么进化" in user_message
    assert "本地洛克王国资料" in user_message
    assert "匹配精灵：画精灵" in user_message


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


@pytest.mark.asyncio
async def test_request_ai_reply_rejects_invalid_json_response() -> None:
    settings = BotSettings(ai_api_key="secret")
    client = FakeClient(InvalidJsonResponse({}))

    with pytest.raises(AIReplyError, match="invalid response"):
        await request_ai_reply("你好", settings=settings, client=client)


# ---------------------------------------------------------------------------
# Structured model gateway (S2-AGENT-01/02/07)
# ---------------------------------------------------------------------------

from qq_bot.services.ai_client import (  # noqa: E402
    CapabilityError,
    provider_capabilities,
    request_model_turn,
)


def _tool_call_message(name: str = "lookup_pet", arguments: str = '{"query": "TestPetA"}') -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_request_model_turn_parses_tool_calls() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse(_tool_call_message()))

    response = await request_model_turn(
        messages=[{"role": "user", "content": "TestPetA 的编号"}],
        tools=[{"type": "function", "function": {"name": "lookup_pet"}}],
        tool_choice="auto",
        response_format=None,
        settings=settings,
        client=client,
    )

    assert response.text is None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "lookup_pet"
    assert call.arguments == {"query": "TestPetA"}
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    posted = client.calls[0]["json"]
    assert posted["tools"] == [{"type": "function", "function": {"name": "lookup_pet"}}]
    assert posted["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_request_model_turn_unparseable_arguments_is_explicit_error() -> None:
    settings = BotSettings(ai_api_key="secret")
    client = FakeClient(FakeResponse(_tool_call_message(arguments="不是JSON")))

    with pytest.raises(AIReplyError, match="unparseable tool arguments"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}],
            settings=settings,
            client=client,
        )


@pytest.mark.asyncio
async def test_request_model_turn_rejects_invalid_tool_call_shape() -> None:
    settings = BotSettings(ai_api_key="secret")
    bad_name = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "x" * 65, "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    client = FakeClient(FakeResponse(bad_name))
    with pytest.raises(AIReplyError, match="invalid tool calls"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}], settings=settings, client=client
        )
    non_dict = {
        "choices": [{"message": {"content": None, "tool_calls": ["nope"]}, "finish_reason": "x"}]
    }
    client2 = FakeClient(FakeResponse(non_dict))
    with pytest.raises(AIReplyError, match="invalid tool calls"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}], settings=settings, client=client2
        )


@pytest.mark.asyncio
async def test_request_model_turn_capability_error_when_tools_disabled() -> None:
    settings = BotSettings(ai_api_key="secret", ai_provider_tools_enabled=False)
    client = FakeClient(FakeResponse(_tool_call_message()))

    with pytest.raises(CapabilityError, match="tools capability"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "lookup_pet"}}],
            settings=settings,
            client=client,
        )
    assert client.calls == []  # rejected before any network call


@pytest.mark.asyncio
async def test_request_model_turn_capability_error_when_structured_disabled() -> None:
    settings = BotSettings(ai_api_key="secret", ai_provider_structured_output_enabled=False)
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "{}"}}]}))

    with pytest.raises(CapabilityError, match="structured output"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
            settings=settings,
            client=client,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_request_model_turn_primary_failure_enters_fallback() -> None:
    settings = _fast_retry_settings(
        ai_max_attempts=1,  # single primary try, then fallback
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )
    client = SequenceClient(
        [
            FakeResponse({}, status_code=500),
            FakeResponse({"choices": [{"message": {"content": '{"a": 1}'}}]}),
        ]
    )

    response = await request_model_turn(
        messages=[{"role": "user", "content": "hi"}], settings=settings, client=client
    )
    assert response.text == '{"a": 1}'
    assert len(client.calls) == 2
    assert client.calls[1]["url"] == "https://fallback.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_request_model_turn_usage_missing_is_none() -> None:
    settings = BotSettings(ai_api_key="secret")
    no_usage = {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]}
    response = await request_model_turn(
        messages=[{"role": "user", "content": "hi"}],
        settings=settings,
        client=FakeClient(FakeResponse(no_usage)),
    )
    assert response.text == "好的"
    assert response.usage is None


@pytest.mark.asyncio
async def test_request_model_turn_circuit_open_skips_primary_and_falls_back(monkeypatch) -> None:
    settings = _fast_retry_settings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
    )

    open_breaker = CircuitBreaker(name="ai_primary", failure_threshold=1, recovery_seconds=30)
    await open_breaker.on_failure(TRANSIENT)

    def fake_breaker_for(name: str):
        if name == BREAKER_AI_PRIMARY:
            return open_breaker
        return CircuitBreaker(name=name, failure_threshold=3, recovery_seconds=30)

    monkeypatch.setattr(ai_client, "_breaker_for", fake_breaker_for)
    client = SequenceClient([FakeResponse({"choices": [{"message": {"content": "备用"}}]})])

    response = await request_model_turn(
        messages=[{"role": "user", "content": "hi"}], settings=settings, client=client
    )
    assert response.text == "备用"
    assert len(client.calls) == 1
    assert client.calls[0]["url"] == "https://fallback.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_request_model_turn_empty_response_raises() -> None:
    settings = BotSettings(ai_api_key="secret")
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(AIReplyError, match="empty response"):
        await request_model_turn(
            messages=[{"role": "user", "content": "hi"}], settings=settings, client=client
        )


def test_provider_capabilities_reflect_config() -> None:
    settings = BotSettings(
        ai_provider_tools_enabled=False, ai_provider_structured_output_enabled=False
    )
    caps = provider_capabilities(settings)
    assert caps.tools is False
    assert caps.structured_output is False
    assert caps.usage is True
    full = provider_capabilities(BotSettings())
    assert full.tools is True and full.structured_output is True
