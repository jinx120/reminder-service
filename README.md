# Reminder Service

A self-hosted reminder dashboard that nags you over Telegram until you tap **✅ Done**.

- One FastAPI container: REST API, static dashboard, APScheduler tick, and a
  long-polling Telegram bot, all on one asyncio loop.
- SQLite, volume-mounted at `./data`, survives rebuilds.
- No inbound network exposure needed for the bot (long polling, outbound only).
  Only the dashboard port is served, intended for a private/Tailscale network.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. Message your new bot once — Telegram will not let a bot open a chat with you.
3. Find your chat id:
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool`
   (or message the bot while the service runs and grep the logs for `unauthorised chat`).
4. `cp .env.example .env` and fill in `BOT_TOKEN` and `CHAT_ID`.
5. `docker compose up -d --build`
6. Dashboard: `http://<host>:8765/`

Without `BOT_TOKEN`/`CHAT_ID` the service still boots and logs what it *would*
have sent — useful for local development.

## Behaviour

A reminder is `pending` until acknowledged. Once `due_at` passes, the scheduler
sends a message, then re-sends every `retry_interval_min` until either you
acknowledge it or `max_retries` **total sends** have gone out. One further
interval after the last send it becomes `expired`.

Two things count as an acknowledgement, both immediate:
- Tapping the inline **✅ Done** button.
- Sending any plain text to the bot — this acks the pending reminder you were
  most recently nagged about. No intent parsing; replying at all is the signal.

Messages from any chat id other than `CHAT_ID` are logged and ignored.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | BotFather token; unset disables Telegram |
| `CHAT_ID` | — | The only chat allowed to talk to the bot |
| `DB_PATH` | `data/reminders.db` | SQLite file (`/data/reminders.db` in Docker) |
| `TICK_SECONDS` | `30` | Scheduler interval |
| `DEFAULT_RETRY_INTERVAL_MIN` | `15` | Form default |
| `DEFAULT_MAX_RETRIES` | `4` | Form default |

All timestamps are stored and served as UTC; the dashboard converts to your
browser's local time on both input and display.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/uvicorn app.main:app --reload --port 8765
```

**The app must run with a single worker.** Two workers means two schedulers and
two polling loops: duplicate nags plus a Telegram `getUpdates` conflict.

## Not built (v1 scope)

Multi-user/multi-chat, recurring reminders, SMS fallback, and dashboard auth
beyond network-level access. SMS would slot in behind the scheduler's `Sender`
interface without touching the retry logic.
