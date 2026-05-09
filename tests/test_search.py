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
    assert prompt_needs_search("搜索 OpenAI")
    assert prompt_needs_search("帮我查下 Tavily")
    assert prompt_needs_search("现在天气怎么样")
    assert prompt_needs_search("BTC 价格")
    assert prompt_needs_search("search OpenAI latest news")
    assert prompt_needs_search("today news")
    assert not prompt_needs_search("讲个笑话")
    assert not prompt_needs_search("Python 是什么")


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


def test_format_search_context_limits_sources_for_reply_prompt() -> None:
    context = format_search_context(
        [
            SearchResult("Title One", "https://example.com/one", "First summary"),
            SearchResult("Title Two", "https://example.com/two", "Second summary"),
            SearchResult("Title Three", "https://example.com/three", "Third summary"),
            SearchResult("Title Four", "https://example.com/four", "Fourth summary"),
        ]
    )

    assert "[1] Title One" in context
    assert "[2] Title Two" in context
    assert "[3] Title Three" in context
    assert "Title Four" not in context
