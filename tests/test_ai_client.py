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
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
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
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
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

    assert payload["max_tokens"] == 1200
    assert "上限 1200 字" in payload["messages"][0]["content"]


def test_build_chat_payload_uses_natural_group_chat_style() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("今天新闻", settings)

    system_prompt = payload["messages"][0]["content"]
    assert "像真实群友一样聊天" in system_prompt
    assert "直接回答" in system_prompt
    assert "不要总用“好的”" in system_prompt


def test_build_chat_payload_bans_list_and_service_closers() -> None:
    """S11-QA-VOICE-01（部署者北极星：回复更像人）：@问答禁编点列表、禁客服
    收尾、不主动自称机器人/AI——长短像群友打字。"""
    payload = build_chat_payload("今天新闻", BotSettings(ai_model="test-model"))
    system_prompt = payload["messages"][0]["content"]
    assert "禁止用 1. 2. 3. 这种编点列表" in system_prompt
    assert "不要说“希望这能帮到你”" in system_prompt
    assert "不要主动说自己是机器人或 AI" in system_prompt
    assert "别凑字数" in system_prompt


def test_build_chat_payload_requires_source_section_with_search_context() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload(
        "今天新闻",
        settings,
        search_context="[1] Example\nURL: https://example.com\nSummary: summary",
    )

    system_prompt = payload["messages"][0]["content"]
    assert "随口带一句从哪看到的" in system_prompt
    assert "别用“来源：1. 2. 3.”这种正式列表收尾" in system_prompt
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
async def test_request_model_turn_payload_includes_model() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse(_tool_call_message()))

    await request_model_turn(
        messages=[{"role": "user", "content": "TestPetA 的编号"}],
        tools=[{"type": "function", "function": {"name": "lookup_pet"}}],
        tool_choice="auto",
        response_format=None,
        settings=settings,
        client=client,
    )

    assert client.calls[0]["json"]["model"] == "test-model"


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
    assert client.calls[0]["json"]["model"] == settings.ai_model
    assert client.calls[1]["json"]["model"] == settings.ai_fallback_model


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


# ---- 通用单轮补全 request_completion（S7-AUTO）----


def _completion_payload(content: str) -> dict:
    return {
        "choices": [
            {"message": {"content": content, "role": "assistant"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_request_completion_returns_text_and_posts_expected_payload() -> None:
    client = FakeClient(FakeResponse(_completion_payload("好的呀")))
    settings = _fast_retry_settings(ai_api_key="k", ai_model="test-model")
    reply = await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="usr",
        settings=settings,
        client=client,
        max_tokens=100,
    )
    assert reply == "好的呀"
    body = client.calls[0]["json"]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "usr"}
    assert "response_format" not in body


@pytest.mark.asyncio
async def test_request_completion_json_mode_sets_response_format() -> None:
    client = FakeClient(FakeResponse(_completion_payload('{"a": 1}')))
    settings = _fast_retry_settings(ai_api_key="k", ai_model="test-model")
    await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="usr",
        settings=settings,
        client=client,
        json_mode=True,
    )
    assert client.calls[0]["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_request_completion_empty_model_reuses_settings_model() -> None:
    client = FakeClient(FakeResponse(_completion_payload("好")))
    settings = _fast_retry_settings(ai_api_key="k", ai_model="main-model")
    await ai_client.request_completion(
        system_prompt="sys", user_prompt="usr", settings=settings, client=client
    )
    assert client.calls[0]["json"]["model"] == "main-model"


@pytest.mark.asyncio
async def test_request_completion_falls_back_to_fallback_model() -> None:
    client = SequenceClient(
        [
            HttpErrorResponse(_completion_payload("x")),
            HttpErrorResponse(_completion_payload("x")),
            FakeResponse(_completion_payload("备用")),
        ]
    )
    settings = _fast_retry_settings(
        ai_api_key="k",
        ai_model="primary-model",
        ai_fallback_api_key="fk",
        ai_fallback_model="fallback-model",
    )
    reply = await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="usr",
        settings=settings,
        client=client,
        model="cheap-model",
    )
    assert reply == "备用"
    assert client.calls[0]["json"]["model"] == "cheap-model"
    assert client.calls[-1]["json"]["model"] == "fallback-model"


@pytest.mark.asyncio
async def test_request_completion_requires_api_key() -> None:
    settings = BotSettings(ai_api_key="")
    with pytest.raises(AIReplyError):
        await ai_client.request_completion(
            system_prompt="sys",
            user_prompt="usr",
            settings=settings,
            client=FakeClient(FakeResponse(_completion_payload("x"))),
        )


# ---- 被 @ 链路人设注入（S7-AUTO-09）----


def test_build_chat_payload_injects_persona_when_configured() -> None:
    settings = BotSettings(ai_model="test-model", persona_name="小洛", persona_prompt="毒舌但心软")
    payload = build_chat_payload("你好", settings)
    system = payload["messages"][0]["content"]
    assert "小洛" in system
    assert "毒舌但心软" in system
    assert "不要编造事实" in system  # grounding 约束保留


def test_build_chat_payload_without_persona_keeps_default_style_line() -> None:
    payload = build_chat_payload("你好", BotSettings(ai_model="test-model"))
    system = payload["messages"][0]["content"]
    assert "像真实群友一样聊天" in system
    assert "说话风格" in system  # 内置默认风格仍然注入


# ---- 思考模型兼容（2026-10-24 glm-5.3-flash 主链切换）----


def test_build_chat_payload_thinking_disabled_flag() -> None:
    default_payload = build_chat_payload("你好", BotSettings(ai_model="test-model"))
    assert "thinking" not in default_payload

    disabled = build_chat_payload(
        "你好", BotSettings(ai_model="test-model", ai_thinking_disabled=True)
    )
    assert disabled["thinking"] == {"type": "disabled"}


def test_build_chat_payload_qa_thinking_overrides_global_disable() -> None:
    """S11-QA-THINK-01（部署者北极星：回复更经过考虑）：@问答链显式开思考
    压过全局 disabled；自主链（request_completion）不受影响。"""
    payload = build_chat_payload(
        "你好",
        BotSettings(ai_model="test-model", ai_thinking_disabled=True, ai_qa_thinking_enabled=True),
    )
    assert payload["thinking"] == {"type": "enabled"}

    # 全局未关时开 Q&A 思考同样生效
    payload = build_chat_payload(
        "你好", BotSettings(ai_model="test-model", ai_qa_thinking_enabled=True)
    )
    assert payload["thinking"] == {"type": "enabled"}

    # 关闭开关 → 回落全局语义（disabled/缺席）
    payload = build_chat_payload(
        "你好", BotSettings(ai_model="test-model", ai_thinking_disabled=True)
    )
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_request_completion_keeps_global_thinking_semantics() -> None:
    """自主链不受 ai_qa_thinking_enabled 影响：抢答场景保持关。"""
    settings = BotSettings(
        ai_api_key="secret",
        ai_model="test-model",
        ai_thinking_disabled=True,
        ai_qa_thinking_enabled=True,
    )
    client = FakeClient(FakeResponse(_completion_payload("x")))

    await ai_client.request_completion(
        system_prompt="sys", user_prompt="usr", settings=settings, client=client
    )

    assert client.calls[0]["json"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_request_completion_thinking_disabled_reaches_payload() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model", ai_thinking_disabled=True)
    client = FakeClient(FakeResponse(_completion_payload("x")))

    reply = await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="usr",
        settings=settings,
        client=client,
        max_tokens=100,
    )

    assert reply == "x"
    assert client.calls[0]["json"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_request_completion_without_flag_omits_thinking_param() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse(_completion_payload("x")))

    await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="usr",
        settings=settings,
        client=client,
        max_tokens=100,
    )

    assert "thinking" not in client.calls[0]["json"]


# ---- 视觉（S11-VISION-01）：多模态载荷 / 降级 / 备用链不带图 ----


def test_build_chat_payload_with_images_builds_multimodal_content() -> None:
    settings = BotSettings(ai_model="test-model")

    payload = build_chat_payload("这是什么梗", settings, images=["https://img.example/a.jpg"])

    user_content = payload["messages"][-1]["content"]
    assert isinstance(user_content, list)
    assert user_content[0] == {"type": "text", "text": "这是什么梗"}
    assert user_content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://img.example/a.jpg"},
    }
    assert "图片" in payload["messages"][0]["content"]


def test_build_chat_payload_without_images_keeps_plain_string() -> None:
    payload = build_chat_payload("你好", BotSettings(ai_model="test-model"))
    assert payload["messages"][-1]["content"] == "你好"


def test_build_chat_payload_extra_system_prompt_appended() -> None:
    payload = build_chat_payload(
        "你好", BotSettings(ai_model="test-model"), extra_system_prompt="可用表情候选"
    )
    assert "可用表情候选" in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_request_ai_reply_sends_images_and_relaxed_timeout() -> None:
    settings = BotSettings(
        ai_api_key="secret",
        ai_model="test-model",
        ai_timeout_seconds=30.0,
        ai_qa_vision_timeout_seconds=91.0,
    )
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "看到图了"}}]}))

    reply = await request_ai_reply(
        "看这张图", settings=settings, client=client, images=["https://img.example/a.jpg"]
    )

    assert reply == "看到图了"
    user_content = client.calls[0]["json"]["messages"][-1]["content"]
    assert isinstance(user_content, list)
    assert any(
        block.get("type") == "image_url"
        and block["image_url"]["url"] == "https://img.example/a.jpg"
        for block in user_content
    )
    assert client.calls[0]["timeout"] == 91.0


@pytest.mark.asyncio
async def test_request_ai_reply_without_images_keeps_default_timeout() -> None:
    settings = BotSettings(ai_api_key="secret", ai_model="test-model")
    client = FakeClient(FakeResponse({"choices": [{"message": {"content": "ok"}}]}))

    await request_ai_reply("你好", settings=settings, client=client)

    assert client.calls[0]["timeout"] is None


@pytest.mark.asyncio
async def test_request_ai_reply_degrades_to_text_when_vision_permanently_rejected() -> None:
    """4xx（模型无视觉/图片源失效）→ 同链路去图重试一次，问答不中断。"""
    settings = BotSettings(
        ai_api_key="secret",
        ai_model="test-model",
        ai_max_attempts=2,
        ai_retry_base_delay_seconds=0.001,
        ai_retry_max_delay_seconds=0.002,
        retry_jitter_ratio=0.0,
    )
    client = SequenceClient(
        [
            FakeResponse({"error": "model does not support image input"}, status_code=400),
            FakeResponse({"choices": [{"message": {"content": "文字版回答"}}]}),
        ]
    )

    reply = await request_ai_reply(
        "看这张图", settings=settings, client=client, images=["https://img.example/a.jpg"]
    )

    assert reply == "文字版回答"
    assert len(client.calls) == 2
    first_content = client.calls[0]["json"]["messages"][-1]["content"]
    second_content = client.calls[1]["json"]["messages"][-1]["content"]
    assert isinstance(first_content, list)
    assert isinstance(second_content, str)
    assert not client.calls[1].get("timeout")  # 纯文本重试不再放宽读超时


@pytest.mark.asyncio
async def test_request_ai_reply_transient_failure_keeps_images_through_retries() -> None:
    """瞬时失败走既有重试，不触发降级（图片仍在重试请求里）。"""
    settings = BotSettings(
        ai_api_key="secret",
        ai_model="test-model",
        ai_max_attempts=2,
        ai_retry_base_delay_seconds=0.001,
        ai_retry_max_delay_seconds=0.002,
        retry_jitter_ratio=0.0,
    )
    client = SequenceClient(
        [
            FakeResponse({"error": "boom"}, status_code=500),
            FakeResponse({"choices": [{"message": {"content": "重试成功"}}]}),
        ]
    )

    reply = await request_ai_reply(
        "看这张图", settings=settings, client=client, images=["https://img.example/a.jpg"]
    )

    assert reply == "重试成功"
    assert len(client.calls) == 2
    assert isinstance(client.calls[1]["json"]["messages"][-1]["content"], list)


@pytest.mark.asyncio
async def test_request_ai_reply_fallback_drops_images() -> None:
    """备用模型不承诺视觉：落备用时去掉图片，宁可纯文本也别把问答打死。"""
    settings = BotSettings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
        ai_max_attempts=1,
        ai_retry_base_delay_seconds=0.001,
        ai_retry_max_delay_seconds=0.002,
        retry_jitter_ratio=0.0,
    )
    client = SequenceClient(
        [
            FakeResponse({"error": "down"}, status_code=500),
            FakeResponse({"choices": [{"message": {"content": "备用回答"}}]}),
        ]
    )

    reply = await request_ai_reply(
        "看这张图", settings=settings, client=client, images=["https://img.example/a.jpg"]
    )

    assert reply == "备用回答"
    fallback_call = client.calls[1]
    assert fallback_call["json"]["model"] == "fallback-model"
    assert fallback_call["json"]["messages"][-1]["content"] == "看这张图"


@pytest.mark.asyncio
async def test_request_completion_with_images_builds_multimodal_and_fallback_drops() -> None:
    settings = BotSettings(
        ai_api_key="primary-secret",
        ai_base_url="https://primary.example.com/v1",
        ai_model="primary-model",
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://fallback.example.com/v1",
        ai_fallback_model="fallback-model",
        ai_max_attempts=1,
        ai_retry_base_delay_seconds=0.001,
        ai_retry_max_delay_seconds=0.002,
        retry_jitter_ratio=0.0,
    )
    client = SequenceClient(
        [
            FakeResponse({"error": "down"}, status_code=503),
            FakeResponse({"choices": [{"message": {"content": "带图生成成功不了，纯文本"}}]}),
        ]
    )

    reply = await ai_client.request_completion(
        system_prompt="sys",
        user_prompt="最新消息 [图片]",
        settings=settings,
        client=client,
        images=["https://img.example/a.jpg"],
    )

    assert reply == "带图生成成功不了，纯文本"
    assert isinstance(client.calls[0]["json"]["messages"][-1]["content"], list)
    assert client.calls[1]["json"]["messages"][-1]["content"] == "最新消息 [图片]"


@pytest.mark.asyncio
async def test_build_chat_payload_vision_requests_get_larger_output_cap() -> None:
    """思考模型看图时推理更长，1200 会被思考吃光导致空正文（2026-10-25 线上
    实测 7 次带图 4 次空正文落备用）：带图请求放宽到 2400，纯文本不变。"""
    settings = BotSettings(ai_model="test-model")

    vision_payload = build_chat_payload("看图", settings, images=["https://img.example/a.jpg"])
    text_payload = build_chat_payload("看图", settings)

    assert vision_payload["max_tokens"] == 2400
    assert text_payload["max_tokens"] == 1200


def test_build_chat_payload_teaches_split_for_tsundere_replies() -> None:
    """2026-10-25 线上实测：'烧啥烧…不烧白不烧'挤在同一句里读不出嘴硬。
    提示词改教：先拒后迎要拆两句（换行分隔，第一句短）。"""
    payload = build_chat_payload("你好", BotSettings(ai_model="test-model"))
    system_prompt = payload["messages"][0]["content"]
    assert "拆成两句说" in system_prompt
    assert "换行" in system_prompt
    assert "第一句要短" in system_prompt
