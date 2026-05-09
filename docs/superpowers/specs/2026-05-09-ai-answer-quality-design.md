# AI Answer Quality Design

## Goal

Improve QQ bot AI answers by reducing hallucinated facts, adding current local time awareness, and making source-backed answers clearer.

## Scope

The bot should keep using the current DeepSeek model configuration. This change does not switch models automatically.

## Behavior

- Every AI request includes the computer's current local date and time in the system prompt.
- The system prompt tells the model not to invent facts, URLs, dates, prices, or source details.
- If live or current information is needed, the model should prefer provided web search context.
- If search context is missing or insufficient, the model should say that it has no reliable source instead of guessing.
- When search context is provided, the final answer should end with a `来源：` section containing at most three sources from the provided context.
- Normal chat without search context should not be forced to include sources.
- Replies should retain the natural QQ group style: direct, concise, and not overly formal.

## Verification

- Unit tests should verify local time is injected into the AI payload.
- Unit tests should verify reliability/source rules are present when search context is provided.
- Existing tests should continue to pass.
