# Reminder Service — MCP Connector, Recurrence, and Dashboard Overhaul

**Date:** 2026-08-14
**Status:** Approved for planning
**Builds on:** `docs/superpowers/plans/2026-08-12-reminder-service.md` (the original 12-task build)

## 1. Goal

Make the reminder service usable **without opening the web page**, by exposing it as a
Claude connector (remote MCP server), and close the functional gaps that make it feel
like a v1: no recurring reminders, no snooze, no timezone awareness, and a dashboard
that cannot even mark a reminder done.

Three workstreams, one deploy:

1. **MCP connector** at `/mcp`, serving both claude.ai and Claude Code.
2. **Recurrence, snooze, timezone, quiet hours** — the functional gaps.
3. **Dashboard overhaul** — including the missing complete action.

## 2. Non-goals

Explicitly deferred by the user to a later iteration:

- Tags, lists, or categories
- Priority levels
- Search / sort / filter UI and bulk actions
- Multi-user support, accounts, or per-user timezones
- Authentication (see §4)

Also out of scope: attachments, sub-tasks, streaks/stats, and any push channel other
than Telegram.

## 3. Constraints inherited from the running system

These are load-bearing facts about the deployment. Violating any of them breaks prod.

- **Storage is naive UTC everywhere.** `timeutil.py` is the only conversion boundary.
  This spec does not change it — timezone support is a *presentation and input* concern.
- **`status` is a plain `str` column** holding the enum value, never a SQLAlchemy `Enum`.
  New status-like columns follow the same rule.
- **`--workers 1` is mandatory.** Two workers means two schedulers, two Telegram
  pollers, duplicate nags, and a `getUpdates` conflict. The MCP session manager makes
  this *more* important, not less — it holds per-session state in process memory.
- **`StaticFiles` is mounted last** and owns `/`; anything mounted after it is shadowed.
- **`logging.getLogger("httpx").setLevel(WARNING)`** in `main.py` suppresses PTB leaking
  `BOT_TOKEN` into logs. Do not remove.
- **Prod has live data.** `SQLModel.metadata.create_all()` creates missing *tables* but
  never adds *columns* to an existing table. Schema changes need an explicit migration.

## 4. Authentication posture

**The `/mcp` endpoint ships with no authentication**, matching the existing dashboard.

This was an explicit user decision after being shown the alternatives (bearer token via
`static_headers`, Anthropic egress IP allowlist, full OAuth 2.0 DCR) and the risk:
anyone who discovers the Funnel URL can create, edit, and delete reminders, and can
trigger Telegram notifications. Recorded here so it is not re-litigated, and so the
next reader does not mistake it for an oversight.

Two implementation consequences:

- Do **not** put a token in the connector URL query string. It would be the worst of
  both worlds — no real security, and URLs leak through logs, proxies, and history.
  The MCP authorization spec prohibits it outright.
- Keep the auth seam clean: all MCP requests pass through a single
  `app/mcp_server.py` entry point, so a future `Authorization`-header check is one
  function, not a scattering of edits.

## 5. Architecture

```
                       ┌──────────────────── FastAPI app ────────────────────┐
claude.ai ──Funnel──▶  │  /mcp        FastMCP (Streamable HTTP ASGI sub-app)  │
Claude Code ──LAN───▶  │  /api/*      REST router                            │
browser ──────────▶    │  /           StaticFiles (mounted LAST)             │
                       │      │              │                               │
                       │      └──────┬───────┘                               │
                       │        app/service.py   ← single source of truth    │
                       │             │                                       │
                       │        SQLite (naive UTC)                           │
                       │             ▲                                       │
                       │        APScheduler tick ──▶ Telegram bot            │
                       └─────────────────────────────────────────────────────┘
```

### 5.1 MCP transport

Official `mcp` SDK (pin `mcp>=2.0,<3.0`), using `FastMCP` exposed as a **Streamable
HTTP** ASGI application mounted at `/mcp`.

Rejected alternatives:
- *Hand-rolled JSON-RPC* — reimplementing a spec that is still moving; no upside.
- *`fastmcp` v2 (third-party)* — extra dependency, no capability we need.

Two integration requirements:

1. **Mount `/mcp` before the `StaticFiles` mount**, for the same reason `/api` is
   mounted before it.
2. **Run FastMCP's session manager from the existing `lifespan()`**, alongside the
   scheduler and the Telegram application. FastMCP's own `lifespan` is not invoked when
   its ASGI app is mounted into a host application; failing to start the session manager
   manifests as requests hanging or 500ing on `initialize`.

The exact SDK call surface (`streamable_http_app()`, `session_manager.run()`) must be
confirmed against the installed 2.x version during implementation rather than assumed
from 1.x memory. If the surface differs, adapt — the design does not depend on the
specific method names.

A `MCP_ENABLED` setting (default `true`) allows disabling the mount without a code
change, as an escape hatch if the sub-app ever destabilises the main service.

### 5.2 Service layer extraction

Currently `routers/reminders.py` constructs `Reminder` rows inline, while `service.py`
holds only ack and send bookkeeping. Adding a second consumer (MCP) would duplicate that
logic, and the two copies would drift.

All business operations move into `app/service.py`:

```
create_reminder, list_reminders, get_reminder, update_reminder,
complete_reminder, snooze_reminder, delete_reminder, search_reminders,
due_digest
```

These take a `Session` plus plain arguments, return model objects or raise a small set of
domain errors (`ReminderNotFound`, `ReminderNotPending`, `InvalidRecurrence`,
`SnoozeLimitReached`). The REST router maps those to HTTP status codes; the MCP layer
maps them to tool errors. Neither contains business rules.

`app/logic.py` stays pure and side-effect free — it gains recurrence and quiet-hours
computation but keeps taking primitives and returning decisions.

## 6. Data model

### 6.1 New columns on `reminders`

| Column | Type | Default | Purpose |
|---|---|---|---|
| `recurrence` | `str \| None` | `NULL` | RRULE subset; `NULL` = one-shot |
| `recur_from` | `str` | `"schedule"` | `schedule` \| `completion` |
| `snooze_count` | `int` | `0` | Display, and enforces the snooze cap |

### 6.2 New table `completions`

Recurring reminders roll forward in place, so without this the history of a series is
lost on every completion.

| Column | Type | Notes |
|---|---|---|
| `id` | `int` PK | |
| `reminder_id` | `int` FK → `reminders.id`, indexed | |
| `scheduled_for` | `datetime` | The `due_at` this occurrence was for |
| `completed_at` | `datetime` | When it was resolved |
| `outcome` | `str` | `completed` \| `expired` |

`outcome` is a plain `str` column holding the enum value — same rule as `status`.

### 6.3 Migration

`app/migrations.py`, run from `create_app()` immediately after `create_all()`.

Versioned via SQLite's `PRAGMA user_version`:

- `user_version == 0` → baseline (the current prod schema). Apply step 1.
- Step 1: `ALTER TABLE reminders ADD COLUMN ...` ×3, create `completions`, set
  `user_version = 1`.

Each step is idempotent and wrapped in a transaction. Adding a column to SQLite with a
non-null default is safe and rewrite-free. Existing rows get `recurrence=NULL`,
`recur_from='schedule'`, `snooze_count=0` — i.e. they keep behaving exactly as today,
which is the correctness bar for this migration.

The migration must be tested against a **copy of the real prod schema**, not just a
freshly-created test database, because those two can differ.

## 7. Recurrence

### 7.1 Supported rules

An explicit subset of RRULE, parsed with `dateutil.rrule`:

- `FREQ` ∈ `DAILY` | `WEEKLY` | `MONTHLY` | `YEARLY`
- `INTERVAL` — positive integer
- `BYDAY` — weekly only (e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`)

Anything outside this whitelist is rejected at write time with `InvalidRecurrence` and a
message naming the offending component. Silently accepting an RRULE we do not honour
would be worse than refusing it.

### 7.2 Anchoring — `recur_from`

Both modes are supported because both are correct for different reminders; picking one
would make the other awkward to express.

- **`schedule`** — next occurrence is computed from the *scheduled* time.
  "Bins out every Tuesday" stays on Tuesdays even when acked late.
- **`completion`** — next occurrence is computed from the *completion* time.
  "Water the plants every 3 days" means 3 days after you actually did it.

`BYDAY` with `recur_from=completion` is contradictory (a weekday set has no meaning
relative to an arbitrary completion instant) and is rejected at validation.

For `completion` mode the interval is derived from `FREQ` + `INTERVAL` using
`relativedelta`, so month and year arithmetic stays calendar-correct.

### 7.3 Roll-forward

On **completion** of a recurring reminder:

1. Write a `completions` row with `outcome='completed'`.
2. Compute the next `due_at` per §7.2. For `schedule` mode, advance repeatedly until the
   result is strictly in the future — a series that was missed for a week resumes at the
   next real occurrence rather than firing a backlog.
3. Reset `status='pending'`, `retry_count=0`, `last_sent_at=NULL`, `snooze_count=0`, and
   set the new `due_at`.

On **expiry** (retry budget exhausted) of a recurring reminder, the same roll-forward
runs with `outcome='expired'`. Without this a single missed occurrence would silently
kill the series forever — the failure mode most likely to erode trust in the tool.

Non-recurring reminders are unchanged: complete → `acked`, expire → `expired`, terminal.

## 8. Time handling

### 8.1 Timezone

New `TIMEZONE` setting (IANA name, default `UTC`), distinct from the container's
`TZ=UTC` — the container stays UTC so storage stays UTC.

`TIMEZONE` governs:
- Natural-language date parsing (§9)
- Quiet-hours window evaluation
- Recurrence day boundaries (what "every Tuesday" means)
- The dashboard's default display zone, served via a new `GET /api/config`

An invalid IANA name fails fast at startup rather than silently falling back to UTC.

### 8.2 Quiet hours

`QUIET_HOURS_START` / `QUIET_HOURS_END` (`HH:MM`, both unset = disabled).

Implemented as a short-circuit at the top of `logic.decide()`: inside the window, return
`Action.NOTHING`. Because no send happens, no retry is consumed *and* no expiry is
evaluated — a reminder cannot quietly die overnight. A reminder due at 02:00 with quiet
hours `22:00–08:00` first fires at 08:00.

Windows crossing midnight (`start > end`) are explicitly handled and explicitly tested;
this is the obvious off-by-one and it will be caught by a test, not by inspection.

### 8.3 Snooze

Snoozing pushes `due_at` forward, resets `retry_count=0` and `last_sent_at=NULL`, and
increments `snooze_count`. Status stays `pending`.

- `DEFAULT_SNOOZE_MIN` (default 15) when no duration is given.
- `MAX_SNOOZES` (default 20) caps a single occurrence; exceeding it raises
  `SnoozeLimitReached` rather than allowing an unbounded defer loop.

Exposed in three places: the dashboard, a new Telegram inline button beside **Done**,
and the `snooze_reminder` MCP tool.

## 9. Natural-language dates

Every tool and endpoint that accepts a time takes **either** ISO-8601 **or** natural
language ("tomorrow at 9am", "in 2 hours", "next monday"), resolved with `dateparser`
against `TIMEZONE` and the current time.

Two rules that make this safe rather than magical:

- **Every response echoes the resolved absolute time**, so a misparse is visible
  immediately instead of surfacing as a missed reminder days later.
- **Ambiguous or unparseable input is an error, never a guess.** Returning "I couldn't
  read that date" is strictly better than silently scheduling something for the wrong
  day.

New dependencies: `dateparser`, `python-dateutil` (the latter is already transitively
present but becomes a direct dependency).

## 10. MCP tool surface

Nine tools. Every tool returns the affected reminder(s) with absolute resolved times.

| Tool | Arguments | Notes |
|---|---|---|
| `create_reminder` | `title`, `due_at`, `note?`, `recurrence?`, `recur_from?`, `retry_interval_min?`, `max_retries?` | `due_at` accepts NL or ISO |
| `list_reminders` | `status?`, `limit?` | Defaults to pending |
| `get_reminder` | `reminder_id` | Includes notification history |
| `update_reminder` | `reminder_id`, any mutable field | Pending only |
| `complete_reminder` | `reminder_id` | Rolls a recurring series forward |
| `snooze_reminder` | `reminder_id`, `duration?` | NL duration accepted |
| `delete_reminder` | `reminder_id` | Hard delete, cascades notifications |
| `search_reminders` | `query`, `status?` | Substring over title and note |
| `whats_due` | `window?` (default today) | Digest: overdue, due today, upcoming |

Tool descriptions must state the configured timezone and current local time, so Claude
resolves relative dates correctly even when it does not call the parser.

Domain errors map to MCP tool errors with actionable messages ("reminder 12 is already
acked and cannot be edited"), never bare stack traces.

## 11. REST API changes

Additive and backward-compatible — the existing dashboard keeps working mid-deploy.

- `POST/PATCH /api/reminders` accept `recurrence`, `recur_from`, and NL `due_at`.
- `POST /api/reminders/{id}/complete` — **new**, and the fix for the dashboard's
  missing complete action.
- `POST /api/reminders/{id}/snooze` — new, optional `duration`.
- `GET /api/reminders/{id}` gains `completions`.
- `GET /api/config` — new: timezone, default snooze, quiet hours, for the frontend.
- Read schemas gain `recurrence`, `recur_from`, `snooze_count`, `next_due_at`.

## 12. Dashboard

Vanilla HTML/CSS/JS, no build step — consistent with what exists. `textContent` over
`innerHTML` stays the rule for anything user-supplied.

- **Complete / snooze / edit inline on each card.** Completing from the web is currently
  impossible; this is the single biggest usability gap.
- **Grouped views:** Overdue, Today, Upcoming, Done — replacing the flat status filter.
- **Relative times** ("in 2h", "3 days ago") beside absolute times.
- **Recurrence shown on the card** ("every 3 days") and editable in the form.
- **Undo-delete** via a toast with an undo action, replacing blocking `confirm()`.
- **Toasts** for all success/failure feedback, replacing the single `#error` paragraph.
- **Keyboard shortcuts:** `n` new, `/` focus search, `Esc` close, `?` shortcut help.
- **Light/dark toggle** persisted to `localStorage`, defaulting to the existing
  `prefers-color-scheme` behaviour.
- Reminder times render in `TIMEZONE` from `/api/config`, with the browser zone as
  fallback.

## 13. Telegram

- **Snooze button** added beside Done on every notification, using `DEFAULT_SNOOZE_MIN`.
- Recurring reminders show the recurrence in the message body.
- Existing dual-ack behaviour (inline button + bare text reply) is unchanged.
- The `find_reply_ack_target` semantics — bare replies ack the most recently *nagged*
  pending reminder — are unchanged.

## 14. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TIMEZONE` | `UTC` | IANA name; input parsing and display |
| `QUIET_HOURS_START` | unset | `HH:MM`; unset disables |
| `QUIET_HOURS_END` | unset | `HH:MM` |
| `DEFAULT_SNOOZE_MIN` | `15` | Default snooze duration |
| `MAX_SNOOZES` | `20` | Cap per occurrence |
| `MCP_ENABLED` | `true` | Escape hatch for the `/mcp` mount |

All optional; every default preserves current behaviour, so an unchanged `.env` after
deploy yields an unchanged-behaving service.

## 15. Error handling

- Domain errors are typed exceptions raised by `service.py`, mapped at each edge (HTTP
  status / MCP tool error). No business logic in either adapter.
- Scheduler behaviour is unchanged: a failing send is logged and skipped **without**
  touching that reminder's counters, so the next tick retries rather than burning an
  attempt. One bad reminder never blocks the others.
- Recurrence computation failures on an individual reminder are logged and leave that
  reminder untouched; they never abort the tick for other reminders.
- Migration failure aborts startup loudly. A half-migrated database serving traffic is
  worse than a service that refuses to boot.

## 16. Testing

TDD throughout, extending the existing 79-test suite (estimate ~130 total; the plan
should record actual counts rather than predictions).

Priority areas, chosen because they are where silent wrongness is most likely:

- **Recurrence:** both anchor modes; missed-occurrence catch-up; expiry roll-forward;
  month/year boundary arithmetic; rejected RRULE components.
- **Quiet hours:** inside/outside; the midnight-crossing window; proof that neither
  retries nor expiry advance while deferred.
- **Migration:** against a copy of the real prod schema, asserting existing rows keep
  their behaviour; idempotency on re-run.
- **NL parsing:** relative and absolute forms, and that ambiguous input errors rather
  than guessing.
- **MCP:** each tool's happy path and its domain-error mapping.
- **Snooze:** counter resets, the cap, and the Telegram button path.

## 17. Deployment

1. Rebuild and restart locally; verify the full suite.
2. Commit per task; push to `github.com/jinx120/reminder-service`.
3. On CT 108: `git pull && docker compose build && docker compose up -d --force-recreate`.
   The read-only deploy key means the pull needs no interactive auth.
4. Verify on LAN (`192.168.1.206:8765`) and tailnet.
5. Add the connector on claude.ai pointing at `https://reminder.tail78f4cc.ts.net/mcp`.

**Back out** by reverting the commit and rebuilding; the migration is additive, so an
older image continues to run against a migrated database (it ignores the new columns).

## 18. Risks

| Risk | Handling |
|---|---|
| Public Funnel path unverifiable from this network — the intranet blocks `*.ts.net`, so every local test loops back over the tailnet | User confirms from a phone on cell data once deployed. Cannot be closed from inside. |
| `static_headers` is beta and unused here anyway | Not a blocker; authless was chosen deliberately (§4) |
| MCP SDK 2.x call surface may differ from 1.x | Verify against the installed version at implementation time; pin `mcp>=2.0,<3.0` |
| Migration against live prod data | Idempotent, versioned, tested against a copy of the real schema; additive so rollback is safe |
| MCP session state is per-process | `--workers 1` already mandatory and enforced in the Dockerfile |
| Recurrence roll-forward bug could spam or silently stop a series | Both directions explicitly tested; expiry rolls forward so a missed occurrence cannot kill a series |
