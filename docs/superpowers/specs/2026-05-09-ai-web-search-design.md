# AI Web Search Design

## Goal

Add optional web search to the QQ bot AI flow so current-information questions can be answered with search context before DeepSeek generates the final reply.

## Scope

The v1 implementation uses Tavily Search as the web search provider and keeps DeepSeek as the answer-generation model. Search is enabled only when configured and only for prompts that look like they need current web information.

This does not change NapCatQQ, OneBot, command replies, scheduled messages, or the existing mention-formatting behavior.

## User Behavior

Users continue to ask questions through the existing AI trigger paths:

- `ai 今天有什么新闻`
- `ai 搜索 DeepSeek 最新消息`
- Mentioning the bot with a question

The bot searches only when the prompt contains a current-information/search cue such as `搜索`, `联网`, `查一下`, `最新`, `今天`, `现在`, `新闻`, `价格`, `天气`, or `官网`. Normal chat such as `ai 讲个笑话` keeps the current direct DeepSeek flow.

## Configuration

Add these environment variables:

- `SEARCH_ENABLED=true`: enables the search feature.
- `TAVILY_API_KEY=`: Tavily API key. Must stay out of git.
- `SEARCH_MAX_RESULTS=5`: maximum search results sent to the model.
- `SEARCH_TIMEOUT_SECONDS=10`: Tavily request timeout.

If `SEARCH_ENABLED` is false or `TAVILY_API_KEY` is empty, the bot falls back to normal AI replies.

## Components

Add `src/qq_bot/services/search.py` with:

- `prompt_needs_search(prompt: str) -> bool`: detects search-worthy prompts using simple Chinese/English trigger words.
- `SearchResult`: small typed structure for title, URL, and snippet/content.
- `search_web(query: str, settings: BotSettings, client: AsyncPostClient | None = None) -> list[SearchResult]`: calls Tavily Search API and normalizes the response.
- `format_search_context(results: list[SearchResult]) -> str`: converts results into compact source context for the AI model.

Update `src/qq_bot/config.py` with search settings and helpers:

- `search_enabled: bool`
- `tavily_api_key: str = Field(default="", repr=False)`
- `search_max_results: int`
- `search_timeout_seconds: float`
- `has_search_config() -> bool`

Update `src/qq_bot/services/ai_client.py` so `request_ai_reply()` can optionally include search context. The final answer prompt should instruct the model to answer in Chinese, be concise, use search context when present, and include source links when useful.

Update `src/qq_bot/plugins/ai_chat.py` to:

1. Determine whether the user prompt needs search.
2. Try Tavily search when enabled and configured.
3. Call DeepSeek with search context when search succeeds.
4. Fall back to normal DeepSeek reply when search is disabled, unconfigured, empty, or fails.
5. Preserve existing named-mention replacement on the final AI reply.

## Error Handling

- Missing `TAVILY_API_KEY`: no startup failure; direct AI fallback.
- Tavily timeout or HTTP failure: log the failure and direct AI fallback.
- Invalid Tavily response: log the failure and direct AI fallback.
- Empty search results: direct AI fallback.
- DeepSeek failure after search: keep the existing user-facing message `AI 服务暂时不可用，请稍后再试。`

## Testing

Add tests for:

- Search trigger detection.
- Search config parsing and secret-safe `repr`.
- Tavily request URL, headers, payload, and result normalization.
- Search context formatting.
- AI payload includes search context when provided.
- AI plugin uses search only for trigger prompts.
- Search failure falls back to direct AI.
- Existing AI reply mention formatting still applies.

Run full verification after implementation:

- `\.\.venv\Scripts\python -m pytest -v`
- `\.\.venv\Scripts\python -m ruff check .`
- `\.\.venv\Scripts\python -c "import bot; print('bot import ok')"`

## Out Of Scope

- Browser automation or scraping arbitrary pages.
- Long-term memory or per-group search history.
- Streaming search or streaming AI replies.
- Model tool-calling orchestration.
- Multiple search providers in v1.
