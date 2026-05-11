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


class InvalidJsonResponse(FakeResponse):
    def json(self) -> dict:
        raise ValueError("not json")


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
        "当前用户问题：继续刚才的话题\n\n"
        "历史聊天记录：\n"
        "用户2001：ai 你好\n"
        "机器人：你好呀"
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
