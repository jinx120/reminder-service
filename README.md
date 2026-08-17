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

## Deployment (current)

Runs as CT 108 `reminder` on the Proxmox node **m91p** (Debian 13, unprivileged,
2 cores / 2 GB / 8 GB, `onboot=1`).

| | |
|---|---|
| LAN | `192.168.1.206` |
| Tailscale | `reminder` — `100.73.241.89` |
| Public URL | `https://reminder.tail78f4cc.ts.net/` (Tailscale Funnel -> `127.0.0.1:8765`) |
| Path | `/opt/reminder-service` |

The container needs two non-default LXC settings, both already applied in
`/etc/pve/lxc/108.conf`:

- `features: nesting=1,keyctl=1` — Docker inside an unprivileged container.
- a bound `/dev/net/tun` (`lxc.cgroup2.devices.allow: c 10:200 rwm` plus the
  mount entry) — without it Tailscale cannot create its interface.

Updating a deployed instance:

```bash
ssh root@192.168.1.206
cd /opt/reminder-service && git pull
docker compose build && docker compose up -d --force-recreate
```

A read-only GitHub deploy key is installed at `/root/.ssh/id_ed25519`, so
`git pull` works without interactive credentials.

**The dashboard is published to the public internet with no authentication.**
That is a deliberate choice, made knowingly — anyone with the URL has full
read/create/delete on your reminders. To make it tailnet-only instead:

```bash
tailscale funnel --https=443 off
tailscale serve --bg 8765
```

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
| `TIMEZONE` | `UTC` | IANA zone name used for natural-language input parsing, quiet hours, recurrence day boundaries, and dashboard/bot display. Storage is always UTC. An invalid name fails fast at startup rather than silently falling back. |
| `QUIET_HOURS_START` | unset | Local-time start of a window in which sends (including retries) are suppressed. Must be set together with `QUIET_HOURS_END`, or not at all — a half-configured window is rejected at startup. Handles a window that crosses midnight. |
| `QUIET_HOURS_END` | unset | Local-time end of the suppression window. |
| `DEFAULT_SNOOZE_MIN` | `15` | Minutes added when a reminder is snoozed without an explicit duration (dashboard, Telegram button, and MCP `snooze_reminder` all fall back to this). |
| `MAX_SNOOZES` | `20` | Snoozes allowed per reminder before further snooze attempts are refused with a cap message. |
| `MCP_ENABLED` | `true` | Mounts the `/mcp` connector (see below). Set `false` to drop the endpoint entirely, with no code change. |

All timestamps are stored and served as UTC; the dashboard converts to your
browser's local time on both input and display.

The six settings above `DEFAULT_MAX_RETRIES` are new in this release and all
optional. An `.env` that predates them is read exactly as before: no quiet
hours, 15-minute snoozes capped at 20, UTC everywhere, and the MCP connector
mounted.

## Recurring reminders

Set a `recurrence` on any reminder using a small RRULE subset:

| Rule | Meaning |
|---|---|
| `FREQ=DAILY` | every day |
| `FREQ=DAILY;INTERVAL=3` | every 3 days |
| `FREQ=WEEKLY;BYDAY=MO,WE,FR` | Mondays, Wednesdays, Fridays |
| `FREQ=MONTHLY` | same day each month |
| `FREQ=YEARLY` | same date each year |

`FREQ` is required and must be `DAILY`, `WEEKLY`, `MONTHLY`, or `YEARLY`.
`BYDAY` works only with `FREQ=WEEKLY`. Anything else is rejected with a message
naming the component — a rule the service will not honour is never silently
accepted.

`recur_from` chooses the anchor:

- `schedule` (default) — the next occurrence follows the *scheduled* time, so
  "bins out every Tuesday" stays on Tuesdays even when acked late.
- `completion` — the next occurrence follows the *completion* time, so
  "water the plants every 3 days" means 3 days after you actually did it.
  `BYDAY` cannot be combined with this anchor.

A recurring reminder rolls forward in place, so `due_at` is always the next
occurrence — there is no separate "next due" field. Each resolved occurrence is
recorded in `completions`, including ones that **expired**: a missed occurrence
rolls the series forward rather than killing it.

## Claude connector (MCP)

The service exposes a remote MCP server at `/mcp` over Streamable HTTP.

Add it in claude.ai under Settings → Connectors → Add custom connector, with the
URL `https://reminder.tail78f4cc.ts.net/mcp`. In Claude Code:
`claude mcp add --transport http reminders https://reminder.tail78f4cc.ts.net/mcp`.

Nine tools are available: `create_reminder`, `list_reminders`, `get_reminder`,
`update_reminder`, `complete_reminder`, `snooze_reminder`, `delete_reminder`,
`search_reminders`, `whats_due`. Every tool accepts natural-language times
("tomorrow at 9am", "in 2 hours") resolved in `TIMEZONE`, and every response
echoes the resolved absolute time.

**There is no authentication on `/mcp`**, matching the dashboard. Anyone who
knows the Funnel URL can create, edit, and delete reminders and trigger Telegram
notifications. This is a deliberate choice for a single-user service on an
unguessable hostname. Set `MCP_ENABLED=false` to drop the endpoint entirely.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/uvicorn app.main:app --reload --port 8765
```

302 tests, all passing, no warnings, as of this writing.

**The app must run with a single worker.** Two workers means two schedulers and
two polling loops: duplicate nags plus a Telegram `getUpdates` conflict.

## Not built (v1 scope)

Multi-user/multi-chat, SMS fallback, and auth on the dashboard or the MCP
connector beyond network-level access (see "Claude connector" above for that
tradeoff). SMS would slot in behind the scheduler's `Sender` interface without
touching the retry logic.
