# QQ Group Bot

This project is a QQ group bot based on NoneBot2, OneBot v11, and NapCatQQ.

## Features

- `/help`: Show the available bot features.
- `/ping`: Check whether the bot is running. The bot replies with `pong`.
- `/精灵 迪莫` or `/洛克 迪莫`: Query the local Rock Kingdom World pet database and return a static pet card image for found pets.
- Explicit AI chat: Send messages such as `ai 你好` to ask the configured AI model for a reply.
- Scheduled messages: Send configured messages to target QQ groups on a cron schedule.

## Local Installation

Run these commands from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

## Configuration

Edit `.env` after copying it from `.env.example`. Example:

```dotenv
ALLOWED_GROUP_IDS=123456789,987654321
AI_API_KEY=sk-your-api-key
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
AI_TIMEOUT_SECONDS=30
CHAT_MEMORY_PATH=data/chat_memory.sqlite3
CHAT_MEMORY_RETENTION_DAYS=3
CHAT_MEMORY_DEFAULT_TURNS=10
CHAT_MEMORY_MAX_RESULTS=20
SEARCH_ENABLED=false
TAVILY_API_KEY=
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SECONDS=10
SCHEDULED_GROUP_IDS=123456789
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_TIMES=
SCHEDULED_CRON_HOUR=9
SCHEDULED_CRON_MINUTE=0
```

`ALLOWED_GROUP_IDS` controls which groups can use the bot. `SCHEDULED_GROUP_IDS` controls which groups receive scheduled messages.
Set `SCHEDULED_CRON_TIMES` to comma-separated `HH:MM` values, such as `11:00,12:10,16:10,20:10`, to send the same scheduled message multiple times per day. If it is blank, the bot uses `SCHEDULED_CRON_HOUR` and `SCHEDULED_CRON_MINUTE`.

Set `SEARCH_ENABLED=true` to enable smart web search for current or search-related AI prompts, such as `ai 今天有什么新闻` or `ai 搜索 DeepSeek 最新消息`. Create a free Tavily API key at https://app.tavily.com/, put `TAVILY_API_KEY` only in your local `.env`, then restart the bot. Tavily's free plan provides monthly free API credits.

AI chat keeps a local 3-day SQLite memory at `CHAT_MEMORY_PATH`. Normal AI prompts automatically use recent context for the current group user. You can explicitly ask it to reference group history with prompts such as `ai 参考最近20条：总结一下`, `ai 参考 洛克王国 的聊天：我们之前说了什么`, or `ai 参考 @某人 的最近20条：总结他的想法`, where `@某人` means using a real QQ @ mention.

## Run The Bot

```powershell
.\.venv\Scripts\python bot.py
```

By default, the bot listens on `127.0.0.1:8080`.

Configure NapCatQQ's OneBot v11 reverse WebSocket URL as:

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

If NapCatQQ and the bot are not running on the same machine, replace `127.0.0.1` with the bot machine's LAN or server address.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -v
```

## Pet Card Images

Generate static Rock Kingdom World pet card images before starting the bot:

```powershell
.\.venv\Scripts\python scripts\generate_roco_pet_cards.py
```

The command writes PNG files to `data/roco_pet_cards/`. Pet lookup commands send these existing files; if a card file is missing, the bot falls back to the text pet record.

Pet art assets are stored locally under `data/roco_assets/`. The Dimo asset and card data are sourced from the Rock Kingdom Mobile BWiki page at https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB and are credited on generated cards. BWiki text data is marked as CC BY-NC-SA 4.0 on the source page.

## Manual Verification

After starting the bot and connecting NapCatQQ:

- Send `/ping` in an allowed group and expect `pong`.
- Send `/help` in an allowed group and expect the feature list.
- Send `/精灵 迪莫` in an allowed group and expect a static pet card image. Send `/精灵 不存在` and expect a not-found text reply.
- Send `ai 你好` in an allowed group and expect an AI reply.
- Enable `SEARCH_ENABLED=true` and set `TAVILY_API_KEY` in `.env`, restart the bot, send `ai 搜索 DeepSeek 最新消息`, and expect an answer using web-search context.
- Send `ai 我喜欢迪莫`, then send `ai 我刚才说我喜欢谁` from the same QQ user in the same group, and expect the reply to use the recent context.
- Send several normal group messages, then send `ai 参考最近5条：总结一下`, and expect the reply to reference those recent group messages.
- Send `ai 参考 @某人 的最近5条：总结他的观点` using a real QQ @ mention, and expect the reply to use that mentioned user's messages when present.
- Temporarily set `SCHEDULED_CRON_TIMES` to the next few minutes, restart the bot, and expect `SCHEDULED_MESSAGE` in the target group. If `SCHEDULED_CRON_TIMES` is blank, use `SCHEDULED_CRON_HOUR` and `SCHEDULED_CRON_MINUTE` instead.
