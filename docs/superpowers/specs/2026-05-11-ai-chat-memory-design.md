# AI Chat Memory Design

## Goal

Improve AI chat continuity by allowing the QQ bot to read recent and explicitly selected chat history instead of answering every prompt as an isolated message.

## Scope

This design covers group-chat text memory for the existing AI chat plugin. It does not add long-term semantic profiles, cross-platform storage, or cloud sync. Memory is local to the bot instance and is retained for 3 days by default.

## Storage

Add a chat memory service, likely `qq_bot.services.chat_memory`, backed by a local SQLite database at `data/chat_memory.sqlite3` by default.

The table stores one row per captured group message:

- `id`: auto-incrementing primary key.
- `group_id`: QQ group ID.
- `user_id`: sender QQ ID.
- `message_text`: plain text message content.
- `created_at`: ISO timestamp.
- `is_ai_prompt`: whether this user message triggered the AI plugin.
- `ai_reply`: optional AI reply text for AI-triggering messages.

The service creates the database and table on first use. It also deletes records older than 3 days during normal write or query operations so retention is automatic without a separate scheduled job.

## Message Capture

The existing `ai_chat` plugin records plain text messages from allowed groups. Non-AI messages are stored as normal group history. AI-triggering user messages are stored with `is_ai_prompt=true`; after the AI reply is produced, the stored row is updated with `ai_reply` or an equivalent paired reply record is written.

Memory failures must not break chat. If SQLite writes fail, the plugin logs the error and continues. If reads fail, the bot falls back to the current no-history behavior.

## Default Context

For normal `ai ...` prompts and direct @bot prompts, the bot automatically reads only the current `group_id + user_id` history. This keeps normal replies personal to that group user and avoids injecting unrelated group chatter.

The default context should include recent AI conversation turns, limited to about 10 recent turns initially. This limit can become configurable later if needed.

## Explicit History References

Users can ask the bot to reference broader history using lightweight natural command patterns inside the AI prompt. The parser removes the history-reference phrase before sending the actual question to the model.

Supported initial patterns:

- Recent group history: `ai 参考最近20条：继续总结`
- Keyword history: `ai 参考 洛克王国 的聊天：我们之前说了什么`
- Mentioned user history: `ai 参考 @小明 的最近20条：总结他的想法`
- Mentioned user plus keyword: `ai 参考 @小明 关于 洛克王国 的聊天：整理重点`

Mentioned users are identified through OneBot `@` message segments, not nickname text matching. This avoids ambiguity from duplicate or changing group nicknames.

If no matching history is found, the context says that no relevant history was found. The model must not invent historical messages.

## AI Payload

Extend `qq_bot.services.ai_client.build_chat_payload()` and `request_ai_reply()` with a `chat_context` argument.

When chat context exists, the payload includes it alongside the current user question. If search context also exists, both are included. Search context remains the primary source for current facts; chat context is used to understand prior conversation and user intent.

The system prompt gains rules telling the model:

- Treat chat history as contextual reference, not guaranteed external fact.
- Do not invent missing history.
- If history is absent or insufficient, say so briefly.
- Keep the existing concise QQ group style.

## Error Handling

- Empty prompt handling remains unchanged.
- Missing AI API configuration remains unchanged.
- Search failure remains a non-fatal fallback.
- Memory write failure is logged and ignored.
- Memory read failure is logged and ignored.
- AI API failure still returns the current unavailable-service message.

## Testing

Add unit tests for the memory service:

- Database initialization and message insertion.
- Reading recent history by `group_id + user_id`.
- Reading recent group history with a limit.
- Keyword search within one group.
- Mentioned-user search using user IDs.
- Deleting records older than 3 days.

Update AI client tests:

- `build_chat_payload()` includes chat history when `chat_context` is provided.
- Search context and chat context can coexist in one payload.
- System prompt includes the no-invented-history rule.

Update AI chat plugin tests:

- Normal AI prompts pass current group-user context to `request_ai_reply()`.
- Explicit recent-history references query group history and strip the reference phrase from the actual question.
- Explicit `@user` references query that user's messages.
- Memory failures do not prevent the AI reply.

## Acceptance Criteria

- The bot remembers recent AI conversation context per group user by default.
- Users can explicitly reference recent N messages, keyword-matched history, and `@user` messages.
- Memory persists across bot restarts through local SQLite storage.
- Records older than 3 days are removed automatically.
- Existing AI chat, web search, and named mention formatting behavior continues to work.
