# QQ Group Bot

This project is a QQ group bot based on NoneBot2, OneBot v11, and NapCatQQ.

## Features

- `/help`: Show the available bot features.
- `/ping`: Check whether the bot is running. The bot replies with `pong`.
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
SCHEDULED_GROUP_IDS=123456789
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_TIMES=
SCHEDULED_CRON_HOUR=9
SCHEDULED_CRON_MINUTE=0
```

`ALLOWED_GROUP_IDS` controls which groups can use the bot. `SCHEDULED_GROUP_IDS` controls which groups receive scheduled messages.
Set `SCHEDULED_CRON_TIMES` to comma-separated `HH:MM` values, such as `11:00,12:10,16:10,20:10`, to send the same scheduled message multiple times per day. If it is blank, the bot uses `SCHEDULED_CRON_HOUR` and `SCHEDULED_CRON_MINUTE`.

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

## Manual Verification

After starting the bot and connecting NapCatQQ:

- Send `/ping` in an allowed group and expect `pong`.
- Send `/help` in an allowed group and expect the feature list.
- Send `ai 你好` in an allowed group and expect an AI reply.
- Temporarily set `SCHEDULED_CRON_TIMES` to the next few minutes, restart the bot, and expect `SCHEDULED_MESSAGE` in the target group. If `SCHEDULED_CRON_TIMES` is blank, use `SCHEDULED_CRON_HOUR` and `SCHEDULED_CRON_MINUTE` instead.
