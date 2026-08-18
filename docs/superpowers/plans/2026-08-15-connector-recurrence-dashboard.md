# MCP Connector, Recurrence, and Dashboard Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the reminder service as an authless Claude connector at `/mcp`, add recurrence / snooze / timezone / quiet hours, and overhaul the dashboard so a reminder can finally be completed from the web.

**Architecture:** All business rules move into `app/service.py`, which raises typed domain errors; `app/logic.py` stays pure and gains recurrence + quiet-hours computation. Three adapters sit on top of that seam — the REST router, a new `app/mcp_server.py` (official `mcp` SDK, Streamable HTTP), and the Telegram bot — and none of them contains business rules. Storage stays naive UTC; timezone is purely an input-parsing and display concern.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, APScheduler, python-telegram-bot, `mcp` 2.0, `dateparser`, `python-dateutil`, vanilla HTML/CSS/JS (no build step).

**Spec:** `docs/superpowers/specs/2026-08-14-reminder-connector-and-polish-design.md`

## Global Constraints

Copied from spec §3, §4, §14. Every task's requirements implicitly include these.

- **Storage is naive UTC everywhere.** `app/timeutil.py` is the only conversion boundary. Never store an aware datetime.
- **`status` and every status-like column is a plain `str` column** holding the enum *value*. Never a SQLAlchemy `Enum` type — it validates by member name and silently breaks on value-based writes.
- **`--workers 1` is mandatory** (Dockerfile CMD). Two workers means two schedulers, two Telegram pollers, duplicate nags, a `getUpdates` conflict, and split MCP session state.
- **`StaticFiles` is mounted last** in `create_app()` and owns `/`. Anything mounted after it is shadowed. `/mcp` and `/api` must be registered before it.
- **`logging.getLogger("httpx").setLevel(logging.WARNING)`** in `app/main.py` stops python-telegram-bot leaking `BOT_TOKEN` into logs. Do not remove.
- **Prod has live data.** `SQLModel.metadata.create_all()` creates missing *tables* but never adds *columns*. Schema changes need `app/migrations.py`.
- **`/mcp` ships with no authentication** — an explicit, recorded user decision (spec §4). Do not add a token to the connector URL query string. Keep all MCP requests flowing through the single `app/mcp_server.py` entry point so a future `Authorization` check is one function.
- **Every new setting is optional and its default preserves current behaviour**, so an unchanged `.env` after deploy yields an unchanged-behaving service.
- **Every response that carries a time echoes the resolved absolute time**, so a natural-language misparse is visible immediately (spec §9).
- **Ambiguous or unparseable date input is an error, never a guess** (spec §9).
- New settings and defaults: `TIMEZONE=UTC`, `QUIET_HOURS_START` unset, `QUIET_HOURS_END` unset, `DEFAULT_SNOOZE_MIN=15`, `MAX_SNOOZES=20`, `MCP_ENABLED=true`.
- Dependency pins: `mcp>=2.0,<3.0`, `dateparser>=1.4,<2.0`, `python-dateutil>=2.9,<3.0`.
- Run tests with `.venv/bin/python -m pytest`. Baseline is **79 passing tests** at commit `2971727`.
- **Docker rebuild after every code change** (standing project rule): `docker compose build && docker compose up -d --force-recreate` — but see Task 18; during tasks 1–17 the local suite is the gate and a rebuild is only needed when a task's verification step says so.

## Facts already verified — do NOT re-derive

These were confirmed empirically against the installed versions. Trust them.

- Installed: `mcp==2.0.0`, `dateparser==1.4.2`, `python-dateutil==2.9.0.post0`, `starlette==1.6.0`, `fastapi==0.141.1`, `python-telegram-bot==22.8`.
- **MCP SDK 2.0 differs from 1.x.** `FastMCP` is gone. Use `from mcp.server.mcpserver import MCPServer`. The decorator is `@mcp.tool(description=...)` and it returns the function unchanged.
- `MCPServer.streamable_http_app(*, streamable_http_path="/mcp", transport_security=None, ...) -> Starlette`. It **constructs** `mcp.session_manager` as a side effect, so it must be called before `.session_manager` is read.
- **LANDMINE:** `transport_security` defaults to DNS-rebinding protection that allows only localhost `Host` headers, so every request — including from `TestClient` — returns 421 "Invalid Host header". Must pass `TransportSecuritySettings(enable_dns_rebinding_protection=False)` from `mcp.server.transport_security`.
- `app.mount("/mcp", ...)` makes bare `/mcp` a 307 redirect to `/mcp/` (Starlette `Mount` never matches the bare prefix). **Verified working alternative that serves `/mcp` exactly with 200 and no redirect:** append `Route("/mcp", endpoint=StreamableHTTPASGIApp(mcp.session_manager))` to `app.router.routes`. `StreamableHTTPASGIApp` lives in `mcp.server.streamable_http_manager`, **not** `mcp.server.streamable_http`. Omit `methods=` so all HTTP methods reach it.
- `async with mcp.session_manager.run():` inside the existing `lifespan()` starts the session manager. FastMCP's own lifespan is not invoked when mounted into a host app.
- `await mcp.call_tool(name, args)` **raises** `mcp.server.mcpserver.exceptions.ToolError` rather than returning `is_error=True`. Message format is exactly `Error executing tool <name>: <your message>`. Tests use `pytest.raises(ToolError, match=...)` and need no HTTP.
- A tool returning a plain `dict` yields `structured_content=None` with the JSON in `content[0].text`. Tests must `json.loads(result.content[0].text)`.
- Result fields are snake_case: `CallToolResult.is_error`, `.structured_content`, `MCPTool.input_schema`.
- **dateparser** with settings `{"TIMEZONE": tz, "TO_TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DATES_FROM": "future", "RELATIVE_BASE": now_local}` correctly parses "tomorrow at 9am", "in 2 hours", "in 30 minutes", "30m", "friday 18:00", "next week"; returns `None` for garbage and `""`.
- **`"next monday"` returns `None`** even though bare `"monday"` parses. Task 4 pre-normalises `next <weekday>` → `<weekday>`.
- **Real prod schema at CT 108 is `PRAGMA user_version = 0`** with exactly the DDL reproduced verbatim in Task 3. That DDL is the migration test fixture.

## File Structure

**New files**

| File | Responsibility |
|---|---|
| `app/errors.py` | Every typed domain error, in one place, importable by `logic.py`, `timeutil.py`, `service.py`, and both adapters. |
| `app/migrations.py` | `PRAGMA user_version`-versioned, idempotent, transactional schema steps. Run from `create_app()`. |
| `app/mcp_server.py` | The only MCP entry point: builds an `MCPServer` with nine tools that delegate to `service.py`. The future auth seam. |
| `tests/test_errors.py` | *(not created — errors are covered by the tests of the code that raises them)* |
| `tests/test_migrations.py` | Migration against a copy of the real prod schema; idempotency. |
| `tests/test_recurrence.py` | `parse_recurrence` / `validate_recurrence` / `next_occurrence`. |
| `tests/test_quiet_hours.py` | `in_quiet_hours` and `decide()`'s short-circuit. |
| `tests/test_parsing.py` | ISO and natural-language date + duration parsing. |
| `tests/test_mcp.py` | Each tool's happy path and its domain-error mapping. |
| `static/style.css` | Dashboard styles, lifted out of the inline `<style>` block, which has outgrown it. |

**Modified files**

| File | Change |
|---|---|
| `app/config.py` | Five new settings with fail-fast validation. |
| `app/models.py` | Three new `Reminder` columns; `Completion` model; `CompletionOutcome` enum. |
| `app/timeutil.py` | `to_local_naive`, `from_local_naive`, `parse_when`, `parse_duration_minutes`. |
| `app/logic.py` | Quiet-hours short-circuit in `decide()`; recurrence parsing and next-occurrence computation. Stays pure. |
| `app/service.py` | Grows from 4 bookkeeping helpers to the full business layer (spec §5.2). |
| `app/scheduler.py` | Passes settings into `decide()`; rolls a recurring series forward on expiry. |
| `app/schemas.py` | Recurrence/snooze fields, `CompletionRead`, `ConfigRead`; `due_at` becomes a `str` resolved by `parse_when`. |
| `app/routers/reminders.py` | Reduced to a thin HTTP adapter over `service.py`; three new endpoints. |
| `app/bot.py` | Snooze inline button; recurrence in the message body; takes `Settings`. |
| `app/main.py` | Runs migrations; mounts `/mcp`; registers domain-error handlers; starts the MCP session manager in `lifespan()`. |
| `static/index.html` | Restructured for grouped views, inline card actions, toasts, theme toggle. |
| `static/app.js` | Rewritten around `/api/config`, grouped rendering, and the new endpoints. |
| `tests/conftest.py` | A `settings` fixture. |
| `requirements.txt`, `.env.example`, `README.md` | New deps, new settings, new docs. |

## Plan-level decisions

Three points where the spec left a gap. Recorded here so they are not re-litigated mid-execution.

1. **Domain errors live in `app/errors.py`, not `app/service.py`.** Spec §5.2 says `service.py` raises them; `logic.py` and `timeutil.py` must raise `InvalidRecurrence` and `InvalidTime` too, and importing those from `service.py` would invert the dependency. `service.py` re-exports them, so `from app.service import ReminderNotFound` still reads naturally.
2. **Tool descriptions state the timezone; tool *results* carry the current local time.** Spec §10 asks descriptions to state both. A description is built once at startup, so an embedded clock would go stale within minutes and actively mislead. Instead every tool description names the configured zone, and every tool result includes `timezone` and `server_time` (local ISO), which is fresh on every call.
3. **The dashboard gets a minimal client-side search box.** Spec §12 lists `/` → focus search, while §2 defers "search / sort / filter UI". The narrowest reading that leaves `/` meaningful: one input that filters the already-loaded cards in the browser. No server-side search UI, no sort controls, no bulk actions.

---

### Task 1: Configuration — five new settings, validated at startup

**Files:**
- Modify: `app/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/conftest.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` gains `timezone: str`, `quiet_hours_start: time | None`, `quiet_hours_end: time | None`, `default_snooze_min: int`, `max_snoozes: int`, `mcp_enabled: bool`, plus properties `tzinfo -> ZoneInfo` and `quiet_hours_enabled -> bool`. `load_settings()` raises `ValueError` on an invalid IANA name, a malformed `HH:MM`, or exactly one of the two quiet-hours bounds being set. New pytest fixture `settings` returning `load_settings()` with a clean environment.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
from datetime import time

import pytest

NEW_KEYS = ("TIMEZONE", "QUIET_HOURS_START", "QUIET_HOURS_END",
            "DEFAULT_SNOOZE_MIN", "MAX_SNOOZES", "MCP_ENABLED")


@pytest.fixture(autouse=True)
def _clean_new_env(monkeypatch):
    for key in NEW_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_new_settings_default_to_current_behaviour():
    s = load_settings()
    assert s.timezone == "UTC"
    assert s.quiet_hours_start is None
    assert s.quiet_hours_end is None
    assert s.quiet_hours_enabled is False
    assert s.default_snooze_min == 15
    assert s.max_snoozes == 20
    assert s.mcp_enabled is True


def test_timezone_is_parsed_into_a_zoneinfo(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/London")
    s = load_settings()
    assert s.timezone == "Europe/London"
    assert str(s.tzinfo) == "Europe/London"


def test_invalid_timezone_fails_fast(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="TIMEZONE"):
        load_settings()


def test_quiet_hours_are_parsed(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    monkeypatch.setenv("QUIET_HOURS_END", "08:00")
    s = load_settings()
    assert s.quiet_hours_start == time(22, 0)
    assert s.quiet_hours_end == time(8, 0)
    assert s.quiet_hours_enabled is True


def test_malformed_quiet_hours_fails_fast(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "10pm")
    monkeypatch.setenv("QUIET_HOURS_END", "08:00")
    with pytest.raises(ValueError, match="QUIET_HOURS_START"):
        load_settings()


def test_half_configured_quiet_hours_fails_fast(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    with pytest.raises(ValueError, match="both"):
        load_settings()


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("FALSE", False), ("0", False), ("no", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
])
def test_mcp_enabled_accepts_common_boolean_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("MCP_ENABLED", raw)
    assert load_settings().mcp_enabled is expected


def test_snooze_settings_override(monkeypatch):
    monkeypatch.setenv("DEFAULT_SNOOZE_MIN", "5")
    monkeypatch.setenv("MAX_SNOOZES", "3")
    s = load_settings()
    assert s.default_snooze_min == 5
    assert s.max_snoozes == 3
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'timezone'`.

- [x] **Step 3: Implement the settings**

Replace `app/config.py` in full:

```python
import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace-only as unset."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean like true/false, got {value!r}")


def _env_timezone(name: str, default: str) -> str:
    """Validate an IANA zone name at startup.

    Failing fast here is deliberate: silently falling back to UTC would move
    every reminder by hours with no visible symptom.
    """
    value = _env(name) or default
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{name} is not a valid IANA timezone: {value!r}") from exc
    return value


def _env_hhmm(name: str) -> time | None:
    value = _env(name)
    if value is None:
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError as exc:
        raise ValueError(f"{name} must be HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    chat_id: int | None
    db_path: str
    tick_seconds: int
    default_retry_interval_min: int
    default_max_retries: int
    timezone: str
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    default_snooze_min: int
    max_snoozes: int
    mcp_enabled: bool

    @property
    def bot_enabled(self) -> bool:
        return self.bot_token is not None and self.chat_id is not None

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def quiet_hours_enabled(self) -> bool:
        return self.quiet_hours_start is not None and self.quiet_hours_end is not None


def load_settings() -> Settings:
    chat_id = _env("CHAT_ID")
    quiet_start = _env_hhmm("QUIET_HOURS_START")
    quiet_end = _env_hhmm("QUIET_HOURS_END")
    if (quiet_start is None) != (quiet_end is None):
        # A half-configured window has no defensible interpretation, and
        # guessing one would silence reminders for an unpredictable stretch.
        raise ValueError(
            "QUIET_HOURS_START and QUIET_HOURS_END must both be set or both be unset"
        )

    return Settings(
        bot_token=_env("BOT_TOKEN"),
        chat_id=int(chat_id) if chat_id is not None else None,
        db_path=_env("DB_PATH") or "data/reminders.db",
        tick_seconds=_env_int("TICK_SECONDS", 30),
        default_retry_interval_min=_env_int("DEFAULT_RETRY_INTERVAL_MIN", 15),
        default_max_retries=_env_int("DEFAULT_MAX_RETRIES", 4),
        timezone=_env_timezone("TIMEZONE", "UTC"),
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        default_snooze_min=_env_int("DEFAULT_SNOOZE_MIN", 15),
        max_snoozes=_env_int("MAX_SNOOZES", 20),
        mcp_enabled=_env_bool("MCP_ENABLED", True),
    )
```

- [x] **Step 4: Add the `settings` fixture**

Append to `tests/conftest.py`:

```python
from app.config import Settings, load_settings


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Default settings with a guaranteed-clean environment.

    Tests that need a different zone or quiet hours build their own with
    `dataclasses.replace(settings, timezone="Europe/London")`.
    """
    for key in ("BOT_TOKEN", "CHAT_ID", "DB_PATH", "TICK_SECONDS",
                "DEFAULT_RETRY_INTERVAL_MIN", "DEFAULT_MAX_RETRIES",
                "TIMEZONE", "QUIET_HOURS_START", "QUIET_HOURS_END",
                "DEFAULT_SNOOZE_MIN", "MAX_SNOOZES", "MCP_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    return load_settings()
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -v && .venv/bin/python -m pytest -q`
Expected: all config tests PASS; the full suite still passes (79 + the new config tests).

- [x] **Step 6: Document the new settings**

Append to `.env.example`:

```
# Timezone for input parsing, quiet hours, recurrence day boundaries, and
# dashboard display. IANA name. Storage stays UTC regardless.
TIMEZONE=UTC
# Suppress all sends inside this local-time window. Both or neither.
# QUIET_HOURS_START=22:00
# QUIET_HOURS_END=08:00
# Snooze
DEFAULT_SNOOZE_MIN=15
MAX_SNOOZES=20
# Escape hatch: set false to drop the /mcp connector without a code change
MCP_ENABLED=true
```

- [x] **Step 7: Commit**

```bash
git add app/config.py tests/test_config.py tests/conftest.py .env.example
git commit -m "feat(config): timezone, quiet hours, snooze, and MCP settings"
```

---

### Task 2: Data model — recurrence columns and the `completions` table

**Files:**
- Modify: `app/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Reminder.recurrence: str | None`, `Reminder.recur_from: str` (default `"schedule"`), `Reminder.snooze_count: int` (default `0`); `class RecurFrom(str, Enum)` with members `schedule`/`completion`; `class CompletionOutcome(str, Enum)` with members `completed`/`expired`; `class Completion(SQLModel, table=True)` with `id`, `reminder_id` (FK, indexed), `scheduled_for: datetime`, `completed_at: datetime`, `outcome: str`.

- [x] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from datetime import datetime

from sqlmodel import select

from app.models import (
    Completion,
    CompletionOutcome,
    RecurFrom,
    Reminder,
)

NOW = datetime(2026, 8, 15, 12, 0, 0)


def test_new_reminder_columns_default_to_one_shot_behaviour(session):
    reminder = Reminder(title="t", due_at=NOW)
    session.add(reminder)
    session.commit()
    session.refresh(reminder)

    assert reminder.recurrence is None
    assert reminder.recur_from == RecurFrom.schedule.value
    assert reminder.snooze_count == 0


def test_reminder_stores_a_recurrence_rule(session):
    reminder = Reminder(
        title="bins",
        due_at=NOW,
        recurrence="FREQ=WEEKLY;BYDAY=TU",
        recur_from=RecurFrom.schedule.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.recurrence == "FREQ=WEEKLY;BYDAY=TU"


def test_completion_row_records_the_occurrence_it_resolved(session):
    reminder = Reminder(title="t", due_at=NOW)
    session.add(reminder)
    session.commit()
    session.refresh(reminder)

    session.add(
        Completion(
            reminder_id=reminder.id,
            scheduled_for=NOW,
            completed_at=NOW,
            outcome=CompletionOutcome.completed.value,
        )
    )
    session.commit()

    row = session.exec(select(Completion)).one()
    assert row.reminder_id == reminder.id
    assert row.scheduled_for == NOW
    assert row.outcome == "completed"


def test_outcome_is_a_plain_string_column_not_an_enum_type(session):
    """The same trap as `status`: a SQLAlchemy Enum validates by member NAME
    and breaks on value-based writes."""
    column = Completion.__table__.columns["outcome"]
    assert column.type.python_type is str
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'Completion' from 'app.models'`.

- [x] **Step 3: Add the columns and the table**

In `app/models.py`, add these enums below `ReminderStatus`:

```python
class RecurFrom(str, Enum):
    schedule = "schedule"
    completion = "completion"


class CompletionOutcome(str, Enum):
    completed = "completed"
    expired = "expired"
```

Add these three fields to `Reminder`, immediately after `created_at`:

```python
    # NULL means one-shot. A whitelisted RRULE subset — see app/logic.py.
    recurrence: str | None = Field(default=None)
    # Plain str column holding the enum value, same rule as `status`.
    recur_from: str = Field(default=RecurFrom.schedule.value)
    snooze_count: int = Field(default=0)
```

Add the new table at the end of the file:

```python
class Completion(SQLModel, table=True):
    """One resolved occurrence of a reminder.

    Recurring reminders roll forward in place, overwriting due_at and status,
    so without this row the history of a series would be lost on every
    completion.
    """

    __tablename__ = "completions"

    id: int | None = Field(default=None, primary_key=True)
    reminder_id: int = Field(foreign_key="reminders.id", index=True)
    scheduled_for: datetime
    completed_at: datetime = Field(default_factory=utcnow)
    # Plain str column holding the enum value, same rule as `status`.
    outcome: str = Field(default=CompletionOutcome.completed.value)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_models.py -v && .venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat(models): recurrence columns and completions table"
```

---

### Task 3: Migration — versioned, idempotent, tested against the real prod schema

**Files:**
- Create: `app/migrations.py`
- Create: `tests/test_migrations.py`
- Modify: `app/main.py:84` (inside `create_app`, right after `create_all()`)

**Interfaces:**
- Consumes: `app.models` (for the target shape).
- Produces: `migrate(engine) -> int` returning the resulting `user_version`; module constant `SCHEMA_VERSION = 1`.

**Why a hand-rolled migration and not Alembic:** one additive step against one SQLite file on one host. Alembic's env/versions scaffolding is more machinery than the whole change.

- [x] **Step 1: Write the failing tests**

Create `tests/test_migrations.py`. `PROD_SCHEMA_DDL` below is the **verbatim** output of `select sql from sqlite_master` on CT 108 — do not "tidy" it, its exact shape is the point of the test.

```python
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.migrations import SCHEMA_VERSION, migrate

PROD_SCHEMA_DDL = [
    """CREATE TABLE reminders (
	id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	note VARCHAR,
	due_at DATETIME NOT NULL,
	retry_interval_min INTEGER NOT NULL,
	max_retries INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	retry_count INTEGER NOT NULL,
	last_sent_at DATETIME,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
)""",
    "CREATE INDEX ix_reminders_status ON reminders (status)",
    "CREATE INDEX ix_reminders_due_at ON reminders (due_at)",
    """CREATE TABLE notifications (
	id INTEGER NOT NULL,
	reminder_id INTEGER NOT NULL,
	sent_at DATETIME NOT NULL,
	acked_at DATETIME,
	telegram_message_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(reminder_id) REFERENCES reminders (id)
)""",
    "CREATE INDEX ix_notifications_reminder_id ON notifications (reminder_id)",
]


@pytest.fixture
def prod_db(tmp_path: Path) -> Path:
    """A database with the exact schema and shape of live CT 108."""
    path = tmp_path / "prod.db"
    connection = sqlite3.connect(path)
    for statement in PROD_SCHEMA_DDL:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO reminders (id, title, note, due_at, retry_interval_min, "
        "max_retries, status, retry_count, last_sent_at, created_at) "
        "VALUES (1, 'existing', NULL, '2026-08-01 09:00:00', 15, 4, "
        "'pending', 2, '2026-08-01 09:30:00', '2026-08-01 08:00:00')"
    )
    connection.commit()
    connection.close()
    return path


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}")


def test_prod_schema_starts_at_version_zero(prod_db):
    with _engine(prod_db).connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar() == 0


def test_migration_adds_the_new_reminder_columns(prod_db):
    engine = _engine(prod_db)
    migrate(engine)
    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(reminders)"))
        }
    assert {"recurrence", "recur_from", "snooze_count"} <= columns


def test_migration_creates_the_completions_table(prod_db):
    engine = _engine(prod_db)
    migrate(engine)
    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(completions)"))
        }
    assert columns == {"id", "reminder_id", "scheduled_for", "completed_at", "outcome"}


def test_existing_rows_keep_behaving_exactly_as_before(prod_db):
    """The correctness bar for this migration: nothing already in the database
    changes meaning."""
    engine = _engine(prod_db)
    migrate(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT title, status, retry_count, recurrence, recur_from, "
                 "snooze_count FROM reminders WHERE id = 1")
        ).one()
    assert row.title == "existing"
    assert row.status == "pending"
    assert row.retry_count == 2
    assert row.recurrence is None
    assert row.recur_from == "schedule"
    assert row.snooze_count == 0


def test_migration_sets_the_schema_version(prod_db):
    engine = _engine(prod_db)
    assert migrate(engine) == SCHEMA_VERSION
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION


def test_migration_is_idempotent(prod_db):
    engine = _engine(prod_db)
    migrate(engine)
    migrate(engine)  # must not raise "duplicate column name"
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
        assert connection.execute(
            text("SELECT count(*) FROM reminders")
        ).scalar() == 1


def test_migration_on_a_fresh_create_all_database_is_a_no_op(db):
    """A brand-new database already has the target shape; the migration must
    still stamp the version so it is never re-run."""
    assert migrate(db.engine) == SCHEMA_VERSION
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrations.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.migrations'`.

- [x] **Step 3: Write the migration**

Create `app/migrations.py`:

```python
import logging

from sqlalchemy import Engine, text

logger = logging.getLogger("reminder.migrations")

SCHEMA_VERSION = 1

# Adding a column with a constant default is rewrite-free in SQLite, so this
# is safe against the live database even with rows in it.
_STEP_1 = [
    "ALTER TABLE reminders ADD COLUMN recurrence VARCHAR",
    "ALTER TABLE reminders ADD COLUMN recur_from VARCHAR NOT NULL DEFAULT 'schedule'",
    "ALTER TABLE reminders ADD COLUMN snooze_count INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS completions (
        id INTEGER NOT NULL,
        reminder_id INTEGER NOT NULL,
        scheduled_for DATETIME NOT NULL,
        completed_at DATETIME NOT NULL,
        outcome VARCHAR NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(reminder_id) REFERENCES reminders (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_completions_reminder_id ON completions (reminder_id)",
]


def _columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}


def migrate(engine: Engine) -> int:
    """Bring the schema up to SCHEMA_VERSION. Returns the resulting version.

    Versioned with SQLite's PRAGMA user_version: 0 is the original prod
    schema. Each step is idempotent and runs in a transaction, so a rerun or
    a crash mid-way leaves a usable database.

    A failure here propagates and aborts startup on purpose — a half-migrated
    database serving traffic is worse than a service that refuses to boot.
    """
    with engine.begin() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar() or 0
        if version >= SCHEMA_VERSION:
            return version

        existing = _columns(connection, "reminders")
        for statement in _STEP_1:
            if statement.startswith("ALTER TABLE reminders ADD COLUMN"):
                column = statement.split()[5]
                if column in existing:
                    continue
            connection.execute(text(statement))

        # PRAGMA does not accept bind parameters.
        connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
        logger.info("migrated schema from user_version %s to %s", version, SCHEMA_VERSION)
        return SCHEMA_VERSION
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrations.py -v`
Expected: all 7 PASS.

- [x] **Step 5: Run the migration at startup**

In `app/main.py`, add the import beside the others:

```python
from app.migrations import migrate
```

and in `create_app()`, immediately after `app.state.db.create_all()`:

```python
    # create_all() adds missing tables but never missing columns, so an
    # existing production database needs this explicit step. A failure here
    # aborts startup deliberately.
    migrate(app.state.db.engine)
```

- [x] **Step 6: Verify the whole suite still passes**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS, including the existing `tests/test_main.py`.

- [x] **Step 7: Commit**

```bash
git add app/migrations.py tests/test_migrations.py app/main.py
git commit -m "feat(db): versioned migration for recurrence columns and completions"
```

---

### Task 4: Domain errors and time parsing (ISO + natural language)

**Files:**
- Create: `app/errors.py`
- Modify: `app/timeutil.py`
- Create: `tests/test_parsing.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app/errors.py`: `ServiceError(Exception)`, and subclasses `ReminderNotFound`, `ReminderNotPending`, `InvalidRecurrence`, `SnoozeLimitReached`, `InvalidTime`, `InvalidField`.
  - `app/timeutil.py`: `to_local_naive(dt: datetime, tz: str) -> datetime`, `from_local_naive(dt: datetime, tz: str) -> datetime`, `parse_when(text: str, *, tz: str, now: datetime | None = None) -> datetime` (returns naive UTC, raises `InvalidTime`), `parse_duration_minutes(text: str) -> int | None`, `as_local_iso(dt: datetime | None, tz: str) -> str | None`.

- [x] **Step 1: Add the dependencies**

Append to `requirements.txt`:

```
dateparser>=1.4,<2.0
python-dateutil>=2.9,<3.0
```

Run: `.venv/bin/pip install -r requirements.txt`
Expected: already satisfied (`dateparser==1.4.2`, `python-dateutil==2.9.0.post0`); the pins just make the transitive dependency direct.

- [x] **Step 2: Write the failing tests**

Create `tests/test_parsing.py`:

```python
from datetime import datetime

import pytest

from app.errors import InvalidTime
from app.timeutil import (
    as_local_iso,
    from_local_naive,
    parse_duration_minutes,
    parse_when,
    to_local_naive,
)

# 2026-08-15 12:00 UTC == 13:00 Europe/London (BST)
NOW = datetime(2026, 8, 15, 12, 0, 0)
LONDON = "Europe/London"


def test_to_local_naive_shifts_into_the_zone():
    assert to_local_naive(NOW, LONDON) == datetime(2026, 8, 15, 13, 0, 0)


def test_from_local_naive_is_the_inverse():
    assert from_local_naive(datetime(2026, 8, 15, 13, 0, 0), LONDON) == NOW


def test_utc_round_trip_is_identity():
    assert from_local_naive(to_local_naive(NOW, "UTC"), "UTC") == NOW


def test_as_local_iso_renders_with_the_zone_offset():
    assert as_local_iso(NOW, LONDON) == "2026-08-15T13:00:00+01:00"


def test_as_local_iso_passes_none_through():
    assert as_local_iso(None, LONDON) is None


def test_iso_with_explicit_offset_is_honoured():
    assert parse_when("2026-09-01T10:00:00+02:00", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 8, 0, 0)


def test_iso_with_z_suffix_is_honoured():
    assert parse_when("2026-09-01T10:00:00Z", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 10, 0, 0)


def test_naive_iso_is_read_in_the_configured_zone():
    """A bare wall-clock string means local wall clock, not UTC — this is the
    single most damaging place to guess wrong."""
    assert parse_when("2026-09-01T10:00:00", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 9, 0, 0)


def test_relative_hours():
    assert parse_when("in 2 hours", tz="UTC", now=NOW) == datetime(2026, 8, 15, 14, 0, 0)


def test_relative_minutes():
    assert parse_when("in 30 minutes", tz="UTC", now=NOW) == datetime(2026, 8, 15, 12, 30, 0)


def test_tomorrow_at_a_time_resolves_in_the_configured_zone():
    # 09:00 London on the 16th is 08:00 UTC.
    assert parse_when("tomorrow at 9am", tz=LONDON, now=NOW) == \
        datetime(2026, 8, 16, 8, 0, 0)


def test_next_weekday_is_normalised_before_parsing():
    """dateparser returns None for "next monday" even though "monday" parses.
    Without normalisation this common phrasing would be a hard error."""
    result = parse_when("next monday", tz="UTC", now=NOW)
    assert result.weekday() == 0
    assert result > NOW


@pytest.mark.parametrize("text", ["", "   ", "asdkjfh", "sometime soonish"])
def test_unparseable_input_raises_rather_than_guessing(text):
    with pytest.raises(InvalidTime):
        parse_when(text, tz="UTC", now=NOW)


def test_the_error_message_quotes_the_offending_input():
    with pytest.raises(InvalidTime, match="asdkjfh"):
        parse_when("asdkjfh", tz="UTC", now=NOW)


@pytest.mark.parametrize("text,expected", [
    ("30m", 30), ("30 min", 30), ("30 minutes", 30),
    ("2h", 120), ("2 hours", 120), ("1h30m", 90),
    ("1d", 1440), ("1 day", 1440), ("45", 45),
])
def test_duration_shorthand(text, expected):
    assert parse_duration_minutes(text) == expected


@pytest.mark.parametrize("text", ["tomorrow at 9am", "", "banana"])
def test_non_durations_return_none_for_the_caller_to_fall_back(text):
    assert parse_duration_minutes(text) is None
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.errors'`.

- [x] **Step 4: Write the error types**

Create `app/errors.py`:

```python
class ServiceError(Exception):
    """Base for every error the business layer raises at its callers.

    Adapters (HTTP router, MCP tools, Telegram handlers) map these to their
    own vocabulary. Nothing below this layer knows about HTTP or MCP.
    """


class ReminderNotFound(ServiceError):
    """No reminder with that id."""


class ReminderNotPending(ServiceError):
    """The reminder is already acked or expired, so it cannot be changed."""


class InvalidRecurrence(ServiceError):
    """The recurrence rule is outside the supported RRULE subset."""


class SnoozeLimitReached(ServiceError):
    """MAX_SNOOZES exceeded for this occurrence."""


class InvalidField(ServiceError):
    """An update named a field that is not editable, or nulled a required one.

    A ServiceError rather than a bare ValueError so it reaches the adapters'
    single error-mapping table instead of surfacing as a 500.
    """


class InvalidTime(ServiceError):
    """A date/time or duration could not be understood.

    Deliberately an error rather than a guess: silently scheduling something
    for the wrong day surfaces days later as a missed reminder.
    """
```

- [x] **Step 5: Extend timeutil**

Replace `app/timeutil.py` in full (the three existing functions are unchanged; the file gains the parsing boundary):

```python
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser

from app.errors import InvalidTime

_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
# dateparser returns None for "next monday" but parses bare "monday" fine.
_NEXT_WEEKDAY = re.compile(rf"^next\s+({_WEEKDAYS})\b", re.IGNORECASE)

_DURATION_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}
_DURATION_TOKEN = re.compile(r"(\d+)\s*([a-z]*)", re.IGNORECASE)
_DURATION_SHAPE = re.compile(r"^(\d+\s*[a-z]*\s*)+$", re.IGNORECASE)

_DATEPARSER_BASE = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "future",
    "TO_TIMEZONE": "UTC",
}


def utcnow() -> datetime:
    """Current UTC time as a naive datetime — the storage form used everywhere."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    """Normalise any datetime to naive UTC.

    A naive input is assumed to already be UTC and passes through untouched.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc_iso(dt: datetime | None) -> str | None:
    """Render a stored naive-UTC datetime as an explicit UTC ISO-8601 string.

    The trailing +00:00 is what lets the browser convert to local time correctly.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def to_local_naive(dt: datetime, tz: str) -> datetime:
    """Stored naive-UTC -> naive wall clock in `tz`."""
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def from_local_naive(dt: datetime, tz: str) -> datetime:
    """Naive wall clock in `tz` -> stored naive UTC."""
    return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)


def as_local_iso(dt: datetime | None, tz: str) -> str | None:
    """Render a stored naive-UTC datetime as an ISO string in `tz`, with offset."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).isoformat()


def parse_when(text: str, *, tz: str, now: datetime | None = None) -> datetime:
    """Resolve ISO-8601 or natural language to naive UTC.

    ISO is tried first so explicit offsets and `Z` are honoured exactly; a
    naive ISO string is read as wall clock in `tz`, not as UTC. Natural
    language falls through to dateparser anchored on `now`.

    Raises InvalidTime rather than guessing — see spec §9.
    """
    if not text or not text.strip():
        raise InvalidTime("No date/time given")
    text = text.strip()
    now = now or utcnow()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        return to_utc_naive(parsed) if parsed.tzinfo else from_local_naive(parsed, tz)

    normalised = _NEXT_WEEKDAY.sub(r"\1", text)
    parsed = dateparser.parse(
        normalised,
        settings={**_DATEPARSER_BASE, "TIMEZONE": tz, "RELATIVE_BASE": to_local_naive(now, tz)},
    )
    if parsed is None:
        raise InvalidTime(f"Could not understand the date/time {text!r}")
    return to_utc_naive(parsed)


def parse_duration_minutes(text: str) -> int | None:
    """Parse a duration shorthand into whole minutes.

    Returns None when the text is not a duration at all, so callers can fall
    back to parse_when for absolute phrasings like "tomorrow at 9am".
    """
    if not text or not text.strip():
        return None
    candidate = text.strip().lower()
    if not _DURATION_SHAPE.match(candidate):
        return None

    total = 0
    for amount, unit in _DURATION_TOKEN.findall(candidate):
        if not unit:
            total += int(amount)  # bare number means minutes
            continue
        if unit not in _DURATION_UNITS:
            return None
        total += int(amount) * _DURATION_UNITS[unit]
    return total or None
```


- [x] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_parsing.py -v`
Expected: all PASS.

- [x] **Step 7: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 8: Commit**

```bash
git add app/errors.py app/timeutil.py tests/test_parsing.py requirements.txt
git commit -m "feat(time): domain errors, timezone helpers, and NL date parsing"
```

---

### Task 5: Quiet hours — a short-circuit at the top of `decide()`

**Files:**
- Modify: `app/logic.py`
- Create: `tests/test_quiet_hours.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `in_quiet_hours(moment: time, start: time | None, end: time | None) -> bool`; `decide()` gains three keyword arguments — `local_now: datetime | None = None`, `quiet_start: time | None = None`, `quiet_end: time | None = None` — all defaulting to today's behaviour.

**Why `local_now` is passed in rather than computed:** `logic.py` stays pure and primitive-taking (spec §5.2). The scheduler owns the conversion.

- [x] **Step 1: Write the failing tests**

Create `tests/test_quiet_hours.py`:

```python
from datetime import datetime, time, timedelta

import pytest

from app.logic import Action, decide, in_quiet_hours

NOW = datetime(2026, 8, 15, 12, 0, 0)
NIGHT = time(22, 0)
MORNING = time(8, 0)


@pytest.mark.parametrize("moment,expected", [
    (time(21, 59), False),
    (time(22, 0), True),
    (time(23, 30), True),
    (time(0, 0), True),
    (time(3, 0), True),
    (time(7, 59), True),
    (time(8, 0), False),      # a reminder deferred overnight fires AT the end
    (time(12, 0), False),
])
def test_window_crossing_midnight(moment, expected):
    assert in_quiet_hours(moment, NIGHT, MORNING) is expected


@pytest.mark.parametrize("moment,expected", [
    (time(8, 59), False),
    (time(9, 0), True),
    (time(16, 59), True),
    (time(17, 0), False),
])
def test_window_within_one_day(moment, expected):
    assert in_quiet_hours(moment, time(9, 0), time(17, 0)) is expected


def test_unset_bounds_disable_the_window():
    assert in_quiet_hours(time(3, 0), None, None) is False
    assert in_quiet_hours(time(3, 0), NIGHT, None) is False
    assert in_quiet_hours(time(3, 0), None, MORNING) is False


def test_zero_width_window_is_disabled_not_always_on():
    assert in_quiet_hours(time(3, 0), time(9, 0), time(9, 0)) is False


def call(**overrides) -> Action:
    kwargs = dict(
        status="pending",
        due_at=NOW - timedelta(minutes=1),
        last_sent_at=None,
        retry_count=0,
        retry_interval_min=15,
        max_retries=4,
        now=NOW,
    )
    kwargs.update(overrides)
    return decide(**kwargs)


def test_due_reminder_is_suppressed_inside_quiet_hours():
    assert call(
        local_now=datetime(2026, 8, 15, 2, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.NOTHING


def test_due_reminder_fires_once_the_window_ends():
    assert call(
        local_now=datetime(2026, 8, 15, 8, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.SEND


def test_quiet_hours_never_expire_a_reminder():
    """No send happens, so no retry is consumed AND no expiry is evaluated —
    a reminder must not be able to quietly die overnight."""
    assert call(
        retry_count=4,
        max_retries=4,
        last_sent_at=NOW - timedelta(hours=5),
        local_now=datetime(2026, 8, 15, 2, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.NOTHING


def test_that_same_reminder_expires_once_the_window_ends():
    assert call(
        retry_count=4,
        max_retries=4,
        last_sent_at=NOW - timedelta(hours=5),
        local_now=datetime(2026, 8, 15, 9, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.EXPIRE


def test_local_now_defaults_to_now_when_omitted():
    assert call(quiet_start=time(11, 0), quiet_end=time(13, 0)) == Action.NOTHING
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_quiet_hours.py -v`
Expected: FAIL with `ImportError: cannot import name 'in_quiet_hours' from 'app.logic'`.

- [x] **Step 3: Implement**

In `app/logic.py`, change the imports to:

```python
from datetime import datetime, time, timedelta
from enum import Enum

from app.models import ReminderStatus
```

Add above `decide()`:

```python
def in_quiet_hours(moment: time, start: time | None, end: time | None) -> bool:
    """Is this local time-of-day inside the configured quiet window?

    A window with start > end crosses midnight (22:00-08:00 is the obvious
    case). The end bound is exclusive, so a reminder deferred overnight fires
    at exactly the end of the window rather than a tick later.
    """
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end
```

Add these three parameters to `decide()`'s signature, after `now: datetime`:

```python
    local_now: datetime | None = None,
    quiet_start: time | None = None,
    quiet_end: time | None = None,
```

and insert this as the **first** body statement, before the status check:

```python
    # Short-circuit before anything else: inside quiet hours no send happens,
    # so no retry is consumed and no expiry is evaluated. A reminder cannot
    # quietly die overnight.
    if in_quiet_hours((local_now or now).time(), quiet_start, quiet_end):
        return Action.NOTHING
```

Extend `decide()`'s docstring with:

```
    `local_now` is the same instant expressed in the configured zone; quiet
    hours are a wall-clock concept, and the caller owns the conversion so
    this module stays pure.
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_quiet_hours.py tests/test_logic.py -v`
Expected: all PASS — the existing `test_logic.py` tests pass unchanged because every new parameter defaults to disabled.

- [x] **Step 5: Commit**

```bash
git add app/logic.py tests/test_quiet_hours.py
git commit -m "feat(logic): quiet-hours short-circuit in decide()"
```

---

### Task 6: Recurrence — validation and next-occurrence computation

**Files:**
- Modify: `app/logic.py`
- Create: `tests/test_recurrence.py`

**Interfaces:**
- Consumes: `app.errors.InvalidRecurrence`, `app.timeutil.to_local_naive`/`from_local_naive`.
- Produces:
  - `parse_recurrence(rule: str) -> dict` returning `{"freq": str, "interval": int, "byday": list[str] | None}`, raising `InvalidRecurrence`.
  - `validate_recurrence(rule: str | None, recur_from: str) -> None` — the write-time gate. `rule=None` is valid (one-shot).
  - `next_occurrence(*, rule: str, recur_from: str, previous_due: datetime, resolved_at: datetime, now: datetime, tz: str = "UTC") -> datetime` — all datetimes naive UTC in and out.

**Verified dateutil behaviour** (do not re-derive; these are the expected values in the tests):

| Case | Result |
|---|---|
| `FREQ=WEEKLY;BYDAY=TU`, dtstart Tue Aug 11, after Aug 15 | Tue Aug 18 |
| same rule, dtstart Tue Aug 4 (a week missed), after Aug 15 | Tue Aug 18 — one occurrence, not a backlog |
| `FREQ=MONTHLY`, dtstart Jan 31 | Mar 31 — rrule *skips* months with no 31st |
| `relativedelta(months=1)` on Jan 31 | Feb 28 — relativedelta *clamps* |
| `relativedelta(years=1)` on Feb 29 2028 | Feb 28 2029 |

The skip/clamp divergence between the two anchor modes is intended: `schedule` means "the 31st", `completion` means "a month after you did it".

- [x] **Step 1: Write the failing tests**

Create `tests/test_recurrence.py`:

```python
from datetime import datetime

import pytest

from app.errors import InvalidRecurrence
from app.logic import next_occurrence, parse_recurrence, validate_recurrence

NOW = datetime(2026, 8, 15, 12, 0, 0)


# --- parsing and validation ---------------------------------------------

def test_minimal_rule_parses():
    assert parse_recurrence("FREQ=DAILY") == \
        {"freq": "DAILY", "interval": 1, "byday": None}


def test_full_rule_parses():
    assert parse_recurrence("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR") == \
        {"freq": "WEEKLY", "interval": 2, "byday": ["MO", "WE", "FR"]}


def test_parsing_is_case_and_space_insensitive():
    assert parse_recurrence(" freq=daily ; interval=3 ")["freq"] == "DAILY"


@pytest.mark.parametrize("rule,offender", [
    ("INTERVAL=2", "FREQ"),
    ("FREQ=HOURLY", "FREQ"),
    ("FREQ=MINUTELY", "FREQ"),
    ("FREQ=DAILY;COUNT=5", "COUNT"),
    ("FREQ=DAILY;UNTIL=20261231T000000Z", "UNTIL"),
    ("FREQ=MONTHLY;BYMONTHDAY=1", "BYMONTHDAY"),
    ("FREQ=DAILY;INTERVAL=0", "INTERVAL"),
    ("FREQ=DAILY;INTERVAL=-1", "INTERVAL"),
    ("FREQ=DAILY;INTERVAL=every", "INTERVAL"),
    ("FREQ=WEEKLY;BYDAY=XX", "BYDAY"),
    ("FREQ=DAILY;BYDAY=MO", "BYDAY"),
    ("not a rule at all", "not a rule at all"),
])
def test_unsupported_components_are_rejected_by_name(rule, offender):
    """Silently accepting an RRULE we do not honour would be worse than
    refusing it — the user would believe a schedule that never runs."""
    with pytest.raises(InvalidRecurrence, match=offender):
        parse_recurrence(rule)


def test_none_is_valid_and_means_one_shot():
    assert validate_recurrence(None, "schedule") is None


def test_byday_with_completion_anchor_is_rejected():
    """A weekday set has no meaning relative to an arbitrary completion
    instant."""
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        validate_recurrence("FREQ=WEEKLY;BYDAY=MO", "completion")


def test_unknown_recur_from_is_rejected():
    with pytest.raises(InvalidRecurrence, match="recur_from"):
        validate_recurrence("FREQ=DAILY", "whenever")


def test_valid_combinations_pass():
    assert validate_recurrence("FREQ=WEEKLY;BYDAY=TU", "schedule") is None
    assert validate_recurrence("FREQ=DAILY;INTERVAL=3", "completion") is None


# --- schedule anchoring --------------------------------------------------

def call(**overrides) -> datetime:
    kwargs = dict(
        rule="FREQ=DAILY",
        recur_from="schedule",
        previous_due=datetime(2026, 8, 15, 9, 0),
        resolved_at=NOW,
        now=NOW,
        tz="UTC",
    )
    kwargs.update(overrides)
    return next_occurrence(**kwargs)


def test_schedule_daily_advances_one_day_from_the_scheduled_time():
    assert call() == datetime(2026, 8, 16, 9, 0)


def test_schedule_ignores_when_it_was_actually_completed():
    """"Bins out every Tuesday" stays on Tuesdays even when acked late."""
    assert call(resolved_at=datetime(2026, 8, 15, 23, 47)) == datetime(2026, 8, 16, 9, 0)


def test_schedule_weekly_byday_lands_on_the_named_weekday():
    result = call(rule="FREQ=WEEKLY;BYDAY=TU", previous_due=datetime(2026, 8, 11, 9, 0))
    assert result == datetime(2026, 8, 18, 9, 0)
    assert result.strftime("%A") == "Tuesday"


def test_schedule_catches_up_rather_than_firing_a_backlog():
    """A series missed for a week resumes at the NEXT real occurrence."""
    assert call(
        rule="FREQ=WEEKLY;BYDAY=TU",
        previous_due=datetime(2026, 8, 4, 9, 0),
    ) == datetime(2026, 8, 18, 9, 0)


def test_schedule_result_is_always_strictly_in_the_future():
    assert call(rule="FREQ=DAILY", previous_due=datetime(2026, 7, 1, 9, 0)) > NOW


def test_schedule_interval_is_honoured():
    assert call(rule="FREQ=DAILY;INTERVAL=3") == datetime(2026, 8, 18, 9, 0)


def test_schedule_monthly_skips_months_without_that_day():
    assert call(
        rule="FREQ=MONTHLY",
        previous_due=datetime(2026, 1, 31, 9, 0),
        now=datetime(2026, 1, 31, 10, 0),
        resolved_at=datetime(2026, 1, 31, 10, 0),
    ) == datetime(2026, 3, 31, 9, 0)


def test_schedule_yearly():
    assert call(rule="FREQ=YEARLY") == datetime(2027, 8, 15, 9, 0)


def test_schedule_weekday_is_evaluated_in_the_configured_zone():
    """23:30 UTC on Monday is 00:30 Tuesday in Berlin, so a Berlin user's
    "every Tuesday" must anchor on the Berlin weekday."""
    result = next_occurrence(
        rule="FREQ=WEEKLY;BYDAY=TU",
        recur_from="schedule",
        previous_due=datetime(2026, 8, 10, 22, 30),   # 00:30 Tue 11th in Berlin
        resolved_at=datetime(2026, 8, 11, 8, 0),
        now=datetime(2026, 8, 11, 8, 0),
        tz="Europe/Berlin",
    )
    assert result == datetime(2026, 8, 17, 22, 30)    # 00:30 Tue 18th in Berlin


# --- completion anchoring ------------------------------------------------

def test_completion_daily_counts_from_when_it_was_done():
    """"Water the plants every 3 days" means 3 days after you actually did it."""
    assert call(
        rule="FREQ=DAILY;INTERVAL=3",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 15, 14, 23),
    ) == datetime(2026, 8, 18, 14, 23)


def test_completion_weekly():
    assert call(
        rule="FREQ=WEEKLY",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 15, 14, 0),
    ) == datetime(2026, 8, 22, 14, 0)


def test_completion_monthly_clamps_into_a_short_month():
    assert call(
        rule="FREQ=MONTHLY",
        recur_from="completion",
        resolved_at=datetime(2026, 1, 31, 14, 23),
        now=datetime(2026, 1, 31, 14, 23),
    ) == datetime(2026, 2, 28, 14, 23)


def test_completion_yearly_clamps_a_leap_day():
    assert call(
        rule="FREQ=YEARLY",
        recur_from="completion",
        resolved_at=datetime(2028, 2, 29, 9, 0),
        now=datetime(2028, 2, 29, 9, 0),
    ) == datetime(2029, 2, 28, 9, 0)


def test_completion_result_is_pushed_past_now_if_resolution_was_stale():
    assert call(
        rule="FREQ=DAILY",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 1, 9, 0),
        now=NOW,
    ) > NOW
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_recurrence.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_recurrence' from 'app.logic'`.

- [x] **Step 3: Implement the recurrence functions**

Add to the imports at the top of `app/logic.py`:

```python
from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

from app.errors import InvalidRecurrence
from app.timeutil import from_local_naive, to_local_naive
```

Add to the bottom of `app/logic.py`:

```python
# The explicit, whitelisted RRULE subset. Anything outside it is rejected at
# write time rather than accepted and quietly ignored.
ALLOWED_FREQ = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
ALLOWED_KEYS = ("FREQ", "INTERVAL", "BYDAY")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
RECUR_FROM_VALUES = ("schedule", "completion")

_COMPLETION_DELTA_UNIT = {
    "DAILY": "days",
    "WEEKLY": "weeks",
    "MONTHLY": "months",
    "YEARLY": "years",
}


def parse_recurrence(rule: str) -> dict:
    """Parse and validate the supported RRULE subset.

    Returns {"freq", "interval", "byday"}. Raises InvalidRecurrence naming the
    offending component, so the user learns what to change.
    """
    parts: dict[str, str] = {}
    for chunk in rule.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise InvalidRecurrence(
                f"Not a recurrence rule: {rule!r} (expected e.g. FREQ=WEEKLY;BYDAY=TU)"
            )
        key, _, value = chunk.partition("=")
        parts[key.strip().upper()] = value.strip().upper()

    for key in parts:
        if key not in ALLOWED_KEYS:
            raise InvalidRecurrence(
                f"{key} is not supported; only {', '.join(ALLOWED_KEYS)} are"
            )

    freq = parts.get("FREQ")
    if freq is None:
        raise InvalidRecurrence("FREQ is required, e.g. FREQ=WEEKLY")
    if freq not in ALLOWED_FREQ:
        raise InvalidRecurrence(
            f"FREQ={freq} is not supported; use one of {', '.join(ALLOWED_FREQ)}"
        )

    raw_interval = parts.get("INTERVAL", "1")
    try:
        interval = int(raw_interval)
    except ValueError:
        raise InvalidRecurrence(f"INTERVAL must be a whole number, got {raw_interval!r}") from None
    if interval < 1:
        raise InvalidRecurrence(f"INTERVAL must be at least 1, got {interval}")

    byday = None
    if "BYDAY" in parts:
        if freq != "WEEKLY":
            raise InvalidRecurrence(f"BYDAY is only supported with FREQ=WEEKLY, not {freq}")
        byday = [code.strip() for code in parts["BYDAY"].split(",") if code.strip()]
        unknown = [code for code in byday if code not in WEEKDAY_CODES]
        if not byday or unknown:
            raise InvalidRecurrence(
                f"BYDAY must be a comma-separated list of {', '.join(WEEKDAY_CODES)}"
            )

    return {"freq": freq, "interval": interval, "byday": byday}


def validate_recurrence(rule: str | None, recur_from: str) -> None:
    """Write-time gate for a (rule, anchor) pair. None means one-shot."""
    if recur_from not in RECUR_FROM_VALUES:
        raise InvalidRecurrence(
            f"recur_from must be one of {', '.join(RECUR_FROM_VALUES)}, got {recur_from!r}"
        )
    if rule is None:
        return
    parsed = parse_recurrence(rule)
    if parsed["byday"] and recur_from == "completion":
        raise InvalidRecurrence(
            "BYDAY cannot be combined with recur_from=completion: a weekday set "
            "has no meaning relative to an arbitrary completion time"
        )


def next_occurrence(
    *,
    rule: str,
    recur_from: str,
    previous_due: datetime,
    resolved_at: datetime,
    now: datetime,
    tz: str = "UTC",
) -> datetime:
    """The next due_at for a recurring reminder. Naive UTC in and out.

    Computed in local wall clock so that day boundaries, month lengths, and
    DST transitions mean what the user means by them.
    """
    parsed = parse_recurrence(rule)
    local_now = to_local_naive(now, tz)

    if recur_from == "completion":
        step = relativedelta(**{_COMPLETION_DELTA_UNIT[parsed["freq"]]: parsed["interval"]})
        candidate = to_local_naive(resolved_at, tz) + step
        # Guard against a stale resolution time leaving the series in the past.
        while candidate <= local_now:
            candidate += step
        return from_local_naive(candidate, tz)

    # schedule anchoring: walk the rule forward from the scheduled time until
    # the result is strictly in the future, so a series missed for a week
    # resumes at the next real occurrence rather than firing a backlog.
    local_previous = to_local_naive(previous_due, tz)
    candidate = rrulestr(rule, dtstart=local_previous).after(
        max(local_previous, local_now), inc=False
    )
    if candidate is None:  # pragma: no cover - the subset has no COUNT/UNTIL
        raise InvalidRecurrence(f"{rule!r} has no further occurrences")
    return from_local_naive(candidate, tz)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_recurrence.py -v`
Expected: all PASS.

- [x] **Step 5: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add app/logic.py tests/test_recurrence.py
git commit -m "feat(logic): whitelisted RRULE subset and next-occurrence computation"
```

---

### Task 7: Service layer — CRUD extracted out of the router

**Files:**
- Modify: `app/service.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Consumes: `app.errors`, `app.logic.validate_recurrence`, `app.timeutil.parse_when`/`to_utc_naive`/`utcnow`.
- Produces (all take `Session` first and commit their own transaction unless noted):
  - `create_reminder(session, *, title, due_at, note=None, recurrence=None, recur_from="schedule", retry_interval_min=15, max_retries=4, tz="UTC", now=None) -> Reminder` — `due_at` is `datetime | str`; a str goes through `parse_when`.
  - `list_reminders(session, *, status=None, limit=None) -> list[Reminder]`
  - `get_reminder(session, reminder_id) -> Reminder` — raises `ReminderNotFound`
  - `update_reminder(session, reminder_id, changes: dict, *, tz="UTC", now=None) -> Reminder`
  - `delete_reminder(session, reminder_id) -> None`
  - `search_reminders(session, query, *, status=None, limit=None) -> list[Reminder]`
  - `MUTABLE_FIELDS: frozenset[str]`
- The four existing helpers (`latest_notification`, `ack_reminder`, `find_reply_ack_target`, `record_send`) keep their current signatures and behaviour. `app/errors` names are re-exported from `app.service`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_service.py` — hoist the new `import` lines to the top of the file rather than leaving them mid-file:

```python
import pytest

from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
)
from app.service import (
    create_reminder,
    delete_reminder,
    get_reminder,
    list_reminders,
    search_reminders,
    update_reminder,
)


def test_create_stores_the_reminder(session):
    reminder = create_reminder(
        session, title="bins", due_at=NOW, note="green one", now=NOW
    )
    assert reminder.id is not None
    assert reminder.title == "bins"
    assert reminder.note == "green one"
    assert reminder.due_at == NOW
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.recurrence is None
    assert reminder.recur_from == "schedule"


def test_create_resolves_a_natural_language_due_at(session):
    reminder = create_reminder(session, title="t", due_at="in 2 hours", now=NOW)
    assert reminder.due_at == NOW + timedelta(hours=2)


def test_create_rejects_an_unparseable_due_at(session):
    with pytest.raises(InvalidTime):
        create_reminder(session, title="t", due_at="whenever-ish", now=NOW)


def test_create_validates_the_recurrence_rule(session):
    with pytest.raises(InvalidRecurrence, match="FREQ"):
        create_reminder(session, title="t", due_at=NOW, recurrence="FREQ=HOURLY", now=NOW)


def test_create_rejects_byday_with_completion_anchor(session):
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        create_reminder(
            session, title="t", due_at=NOW,
            recurrence="FREQ=WEEKLY;BYDAY=MO", recur_from="completion", now=NOW,
        )


def test_list_is_ordered_by_due_then_id(session):
    make_reminder(session, title="late", due_at=NOW + timedelta(hours=2))
    make_reminder(session, title="soon", due_at=NOW + timedelta(minutes=5))
    assert [r.title for r in list_reminders(session)] == ["soon", "late"]


def test_list_filters_by_status_and_honours_limit(session):
    make_reminder(session, title="a")
    make_reminder(session, title="b")
    make_reminder(session, title="c", status=ReminderStatus.acked.value)
    assert len(list_reminders(session, status="pending")) == 2
    assert len(list_reminders(session, limit=1)) == 1


def test_get_raises_for_an_unknown_id(session):
    with pytest.raises(ReminderNotFound, match="404|not found|999"):
        get_reminder(session, 999)


def test_update_applies_only_the_given_fields(session):
    reminder = make_reminder(session, title="old", note="keep")
    updated = update_reminder(session, reminder.id, {"title": "new"}, now=NOW)
    assert updated.title == "new"
    assert updated.note == "keep"


def test_update_resolves_a_natural_language_due_at(session):
    reminder = make_reminder(session)
    updated = update_reminder(session, reminder.id, {"due_at": "in 30 minutes"}, now=NOW)
    assert updated.due_at == NOW + timedelta(minutes=30)


def test_update_refuses_a_resolved_reminder(session):
    reminder = make_reminder(session, status=ReminderStatus.acked.value)
    with pytest.raises(ReminderNotPending, match="acked"):
        update_reminder(session, reminder.id, {"title": "x"}, now=NOW)


def test_update_validates_recurrence_against_the_stored_anchor(session):
    """Changing only the rule must still be checked against the anchor
    already on the row, not against the default."""
    reminder = make_reminder(session, recur_from="completion")
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        update_reminder(session, reminder.id, {"recurrence": "FREQ=WEEKLY;BYDAY=MO"}, now=NOW)


def test_update_can_clear_a_recurrence(session):
    reminder = make_reminder(session, recurrence="FREQ=DAILY")
    updated = update_reminder(session, reminder.id, {"recurrence": None}, now=NOW)
    assert updated.recurrence is None


def test_update_rejects_an_unknown_field(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidField, match="status"):
        update_reminder(session, reminder.id, {"status": "acked"}, now=NOW)


def test_update_refuses_to_null_a_required_field(session):
    """An explicit null on a NOT NULL column must be a 4xx, not a 500."""
    reminder = make_reminder(session)
    with pytest.raises(InvalidField, match="recur_from"):
        update_reminder(session, reminder.id, {"recur_from": None}, now=NOW)


def test_update_can_still_clear_a_note(session):
    reminder = make_reminder(session, note="old")
    assert update_reminder(session, reminder.id, {"note": None}, now=NOW).note is None


def test_delete_removes_the_reminder_and_its_notifications(session):
    reminder = make_reminder(session)
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW))
    session.commit()

    delete_reminder(session, reminder.id)

    assert session.get(Reminder, reminder.id) is None
    assert session.exec(select(Notification)).all() == []


def test_delete_raises_for_an_unknown_id(session):
    with pytest.raises(ReminderNotFound):
        delete_reminder(session, 999)


def test_search_matches_title_and_note_case_insensitively(session):
    make_reminder(session, title="Take the Bins out")
    make_reminder(session, title="call mum", note="about the BINS")
    make_reminder(session, title="unrelated")
    assert {r.title for r in search_reminders(session, "bins")} == \
        {"Take the Bins out", "call mum"}


def test_search_can_be_narrowed_by_status(session):
    make_reminder(session, title="bins now")
    make_reminder(session, title="bins done", status=ReminderStatus.acked.value)
    assert [r.title for r in search_reminders(session, "bins", status="pending")] == \
        ["bins now"]
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'create_reminder' from 'app.service'`.

- [x] **Step 3: Implement**

Replace the imports at the top of `app/service.py` with:

```python
from datetime import datetime

from sqlmodel import Session, or_, select

from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
    ServiceError,
    SnoozeLimitReached,
)
from app.logic import validate_recurrence
from app.models import Completion, CompletionOutcome, Notification, Reminder, ReminderStatus
from app.timeutil import parse_when, to_utc_naive, utcnow

# Re-exported so adapters can `from app.service import ReminderNotFound`
# without needing to know the errors live in their own module.
__all__ = [
    "InvalidField", "InvalidRecurrence", "InvalidTime", "ReminderNotFound",
    "ReminderNotPending", "ServiceError", "SnoozeLimitReached",
    "MUTABLE_FIELDS", "create_reminder", "list_reminders", "get_reminder",
    "update_reminder", "delete_reminder", "search_reminders",
    "latest_notification", "ack_reminder", "find_reply_ack_target", "record_send",
]

MUTABLE_FIELDS = frozenset({
    "title", "note", "due_at", "recurrence", "recur_from",
    "retry_interval_min", "max_retries",
})
# `note` and `recurrence` are the only two a client may legitimately clear.
# Without this guard an explicit JSON null on any other field would reach the
# database as a NOT NULL violation, i.e. a 500 where a 4xx belongs.
CLEARABLE_FIELDS = frozenset({"note", "recurrence"})
```

Add these functions to `app/service.py`, above the existing `latest_notification`:

```python
def _resolve_due(value: datetime | str, *, tz: str, now: datetime | None) -> datetime:
    """Accept either a datetime or a string (ISO or natural language)."""
    if isinstance(value, datetime):
        return to_utc_naive(value)
    return parse_when(value, tz=tz, now=now)


def create_reminder(
    session: Session,
    *,
    title: str,
    due_at: datetime | str,
    note: str | None = None,
    recurrence: str | None = None,
    recur_from: str = "schedule",
    retry_interval_min: int = 15,
    max_retries: int = 4,
    tz: str = "UTC",
    now: datetime | None = None,
) -> Reminder:
    """Create a pending reminder. Raises InvalidTime / InvalidRecurrence."""
    now = now or utcnow()
    validate_recurrence(recurrence, recur_from)
    reminder = Reminder(
        title=title,
        note=note,
        due_at=_resolve_due(due_at, tz=tz, now=now),
        recurrence=recurrence,
        recur_from=recur_from,
        retry_interval_min=retry_interval_min,
        max_retries=max_retries,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def list_reminders(
    session: Session, *, status: str | None = None, limit: int | None = None
) -> list[Reminder]:
    statement = select(Reminder)
    if status is not None:
        statement = statement.where(Reminder.status == status)
    statement = statement.order_by(Reminder.due_at, Reminder.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def get_reminder(session: Session, reminder_id: int) -> Reminder:
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        raise ReminderNotFound(f"Reminder {reminder_id} not found")
    return reminder


def _require_pending(reminder: Reminder) -> None:
    if reminder.status != ReminderStatus.pending.value:
        raise ReminderNotPending(
            f"Reminder {reminder.id} is already {reminder.status} and cannot be changed"
        )


def update_reminder(
    session: Session,
    reminder_id: int,
    changes: dict,
    *,
    tz: str = "UTC",
    now: datetime | None = None,
) -> Reminder:
    """Apply a partial update to a pending reminder."""
    now = now or utcnow()
    reminder = get_reminder(session, reminder_id)
    _require_pending(reminder)

    unknown = set(changes) - MUTABLE_FIELDS
    if unknown:
        raise InvalidField(f"Not an editable field: {', '.join(sorted(unknown))}")

    nulled = {f for f, v in changes.items() if v is None} - CLEARABLE_FIELDS
    if nulled:
        raise InvalidField(f"Cannot be cleared: {', '.join(sorted(nulled))}")

    if "recurrence" in changes or "recur_from" in changes:
        # Validate the *resulting* pair, so changing one field is still
        # checked against the value already stored for the other.
        validate_recurrence(
            changes.get("recurrence", reminder.recurrence),
            changes.get("recur_from", reminder.recur_from),
        )

    if changes.get("due_at") is not None:
        changes = {**changes, "due_at": _resolve_due(changes["due_at"], tz=tz, now=now)}

    for field, value in changes.items():
        setattr(reminder, field, value)

    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def delete_reminder(session: Session, reminder_id: int) -> None:
    """Hard delete, cascading to notifications and completions."""
    reminder = get_reminder(session, reminder_id)
    for notification in session.exec(
        select(Notification).where(Notification.reminder_id == reminder_id)
    ).all():
        session.delete(notification)
    for completion in session.exec(
        select(Completion).where(Completion.reminder_id == reminder_id)
    ).all():
        session.delete(completion)
    session.delete(reminder)
    session.commit()


def search_reminders(
    session: Session,
    query: str,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[Reminder]:
    """Case-insensitive substring match over title and note."""
    pattern = f"%{query}%"
    statement = select(Reminder).where(
        or_(Reminder.title.ilike(pattern), Reminder.note.ilike(pattern))
    )
    if status is not None:
        statement = statement.where(Reminder.status == status)
    statement = statement.order_by(Reminder.due_at, Reminder.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: all PASS.

- [x] **Step 5: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS — the router still has its own inline copies at this point and is untouched.

- [x] **Step 6: Commit**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat(service): CRUD, search, and validation in the business layer"
```

---

### Task 8: Service layer — completion, roll-forward, and the Telegram ack path

**Files:**
- Modify: `app/service.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Consumes: Task 6's `next_occurrence`, Task 7's `get_reminder`/`_require_pending`.
- Produces: `complete_reminder(session, reminder_id, *, tz="UTC", now=None) -> Reminder`; `_roll_forward(session, reminder, *, outcome, resolved_at, tz)` (internal, does not commit); `ack_reminder` gains a `tz: str = "UTC"` keyword and now delegates to `complete_reminder`, so a Telegram ack rolls a series forward exactly like a dashboard or MCP completion.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_service.py`:

```python
from app.models import Completion, CompletionOutcome
from app.service import complete_reminder


def test_completing_a_one_shot_reminder_is_terminal(session):
    reminder = make_reminder(session)
    completed = complete_reminder(session, reminder.id, now=NOW)
    assert completed.status == ReminderStatus.acked.value


def test_completing_writes_a_completion_row(session):
    due = NOW - timedelta(hours=1)
    reminder = make_reminder(session, due_at=due)
    complete_reminder(session, reminder.id, now=NOW)

    row = session.exec(select(Completion)).one()
    assert row.reminder_id == reminder.id
    assert row.scheduled_for == due
    assert row.completed_at == NOW
    assert row.outcome == CompletionOutcome.completed.value


def test_completing_stamps_the_latest_notification(session):
    reminder = make_reminder(session)
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW - timedelta(minutes=5)))
    session.commit()

    complete_reminder(session, reminder.id, now=NOW)

    assert session.exec(select(Notification)).one().acked_at == NOW


def test_completing_a_recurring_reminder_rolls_it_forward_in_place(session):
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)

    assert rolled.status == ReminderStatus.pending.value
    assert rolled.due_at == datetime(2026, 8, 16, 9, 0)


def test_roll_forward_resets_the_per_occurrence_counters(session):
    reminder = make_reminder(
        session,
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=DAILY",
        retry_count=3,
        last_sent_at=NOW - timedelta(minutes=20),
        snooze_count=2,
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)

    assert rolled.retry_count == 0
    assert rolled.last_sent_at is None
    assert rolled.snooze_count == 0


def test_roll_forward_records_the_occurrence_that_was_resolved(session):
    """due_at is overwritten in place, so the completions row is the only
    surviving record of the occurrence."""
    due = datetime(2026, 8, 15, 9, 0)
    reminder = make_reminder(session, due_at=due, recurrence="FREQ=DAILY")
    complete_reminder(session, reminder.id, now=NOW)
    assert session.exec(select(Completion)).one().scheduled_for == due


def test_completion_anchored_recurrence_counts_from_now(session):
    reminder = make_reminder(
        session,
        due_at=datetime(2026, 8, 10, 9, 0),
        recurrence="FREQ=DAILY;INTERVAL=3",
        recur_from="completion",
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)
    assert rolled.due_at == NOW + timedelta(days=3)


def test_completing_an_unknown_reminder_raises(session):
    with pytest.raises(ReminderNotFound):
        complete_reminder(session, 999, now=NOW)


def test_completing_an_already_acked_reminder_raises(session):
    reminder = make_reminder(session, status=ReminderStatus.acked.value)
    with pytest.raises(ReminderNotPending, match="acked"):
        complete_reminder(session, reminder.id, now=NOW)


def test_telegram_ack_rolls_a_series_forward_too(session):
    """The bot's ack path must not be a second, divergent implementation."""
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    assert ack_reminder(session, reminder.id, now=NOW) is True
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)


def test_ack_still_returns_false_instead_of_raising(session):
    """Double-taps on the inline button must stay harmless."""
    reminder = make_reminder(session)
    assert ack_reminder(session, reminder.id, now=NOW) is True
    assert ack_reminder(session, reminder.id, now=NOW) is False
    assert ack_reminder(session, 999, now=NOW) is False
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'complete_reminder' from 'app.service'`.

- [x] **Step 3: Implement**

Add `next_occurrence` to the `app.logic` import in `app/service.py`:

```python
from app.logic import next_occurrence, validate_recurrence
```

and add `"complete_reminder"` to `__all__`.

Add to `app/service.py`, after `search_reminders`:

```python
def _stamp_latest_notification(session: Session, reminder_id: int, now: datetime) -> None:
    notification = latest_notification(session, reminder_id)
    if notification is not None and notification.acked_at is None:
        notification.acked_at = now
        session.add(notification)


def _resolve_occurrence(
    session: Session,
    reminder: Reminder,
    *,
    outcome: str,
    resolved_at: datetime,
    tz: str,
    terminal_status: str,
) -> None:
    """Close out one occurrence, rolling a series forward if there is one.

    Does not commit — the caller owns the transaction boundary, which is what
    lets the scheduler resolve several reminders in a single tick.
    """
    session.add(
        Completion(
            reminder_id=reminder.id,
            scheduled_for=reminder.due_at,
            completed_at=resolved_at,
            outcome=outcome,
        )
    )

    if reminder.recurrence is None:
        reminder.status = terminal_status
        session.add(reminder)
        return

    reminder.due_at = next_occurrence(
        rule=reminder.recurrence,
        recur_from=reminder.recur_from,
        previous_due=reminder.due_at,
        resolved_at=resolved_at,
        now=resolved_at,
        tz=tz,
    )
    reminder.status = ReminderStatus.pending.value
    reminder.retry_count = 0
    reminder.last_sent_at = None
    reminder.snooze_count = 0
    session.add(reminder)


def complete_reminder(
    session: Session,
    reminder_id: int,
    *,
    tz: str = "UTC",
    now: datetime | None = None,
) -> Reminder:
    """Mark an occurrence done. A recurring reminder rolls forward in place."""
    now = now or utcnow()
    reminder = get_reminder(session, reminder_id)
    _require_pending(reminder)

    _stamp_latest_notification(session, reminder_id, now)
    _resolve_occurrence(
        session,
        reminder,
        outcome=CompletionOutcome.completed.value,
        resolved_at=now,
        tz=tz,
        terminal_status=ReminderStatus.acked.value,
    )
    session.commit()
    session.refresh(reminder)
    return reminder
```

Replace the body of the existing `ack_reminder` so there is one implementation of completion, not two:

```python
def ack_reminder(
    session: Session,
    reminder_id: int,
    *,
    now: datetime | None = None,
    tz: str = "UTC",
) -> bool:
    """Telegram's completion path: complete_reminder with a boolean result.

    Returns False (and changes nothing) if the reminder is unknown or already
    resolved, which makes double-taps on the inline button harmless.
    """
    try:
        complete_reminder(session, reminder_id, tz=tz, now=now)
    except (ReminderNotFound, ReminderNotPending):
        return False
    return True
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_service.py tests/test_bot.py -v`
Expected: all PASS, including the pre-existing ack tests and all of `test_bot.py`.

- [x] **Step 5: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat(service): completion with recurring roll-forward, shared with the bot"
```

---

### Task 9: Service layer — snooze, expiry, and the due digest

**Files:**
- Modify: `app/service.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Consumes: Task 8's `_resolve_occurrence`, Task 4's `parse_duration_minutes`/`parse_when`.
- Produces:
  - `snooze_reminder(session, reminder_id, *, duration=None, default_minutes=15, max_snoozes=20, tz="UTC", now=None) -> Reminder`
  - `expire_reminder(session, reminder, *, tz="UTC", now) -> None` — **does not commit**, matching `record_send`, so the scheduler can resolve several reminders in one transaction.
  - `due_digest(session, *, window="today", tz="UTC", now=None) -> dict` returning `{"now": datetime, "horizon": datetime, "overdue": [...], "due_today": [...], "upcoming": [...]}`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_service.py`:

```python
from app.errors import SnoozeLimitReached
from app.service import due_digest, expire_reminder, snooze_reminder


def test_snooze_without_a_duration_uses_the_default(session):
    reminder = make_reminder(session)
    snoozed = snooze_reminder(session, reminder.id, default_minutes=15, now=NOW)
    assert snoozed.due_at == NOW + timedelta(minutes=15)


def test_snooze_accepts_a_duration_shorthand(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, duration="2h", now=NOW).due_at == \
        NOW + timedelta(hours=2)


def test_snooze_accepts_an_absolute_phrase(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, duration="in 45 minutes", now=NOW).due_at == \
        NOW + timedelta(minutes=45)


def test_snooze_stays_pending_and_resets_the_send_counters(session):
    reminder = make_reminder(session, retry_count=3, last_sent_at=NOW - timedelta(minutes=5))
    snoozed = snooze_reminder(session, reminder.id, now=NOW)
    assert snoozed.status == ReminderStatus.pending.value
    assert snoozed.retry_count == 0
    assert snoozed.last_sent_at is None


def test_snooze_increments_the_counter(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, now=NOW).snooze_count == 1
    assert snooze_reminder(session, reminder.id, now=NOW).snooze_count == 2


def test_snooze_is_capped(session):
    """Without a cap a reminder can be deferred forever, which is the same as
    losing it silently."""
    reminder = make_reminder(session, snooze_count=3)
    with pytest.raises(SnoozeLimitReached, match="3"):
        snooze_reminder(session, reminder.id, max_snoozes=3, now=NOW)


def test_snooze_rejects_a_target_in_the_past(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidTime, match="future"):
        snooze_reminder(session, reminder.id, duration="2026-01-01T00:00:00Z", now=NOW)


def test_snooze_rejects_gibberish(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidTime):
        snooze_reminder(session, reminder.id, duration="in a bit", now=NOW)


def test_snooze_refuses_a_resolved_reminder(session):
    reminder = make_reminder(session, status=ReminderStatus.expired.value)
    with pytest.raises(ReminderNotPending):
        snooze_reminder(session, reminder.id, now=NOW)


def test_expiring_a_one_shot_reminder_is_terminal(session):
    reminder = make_reminder(session)
    expire_reminder(session, reminder, now=NOW)
    session.commit()
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.expired.value
    assert session.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


def test_expiring_a_recurring_reminder_rolls_the_series_forward(session):
    """A single missed occurrence must not silently kill the series — that is
    the failure mode most likely to erode trust in the tool."""
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    expire_reminder(session, reminder, now=NOW)
    session.commit()
    session.refresh(reminder)

    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)
    assert reminder.retry_count == 0
    assert session.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


def test_expire_reminder_does_not_commit(session):
    reminder = make_reminder(session)
    expire_reminder(session, reminder, now=NOW)
    session.rollback()
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.pending.value


def test_digest_buckets_by_overdue_today_and_upcoming(session):
    make_reminder(session, title="late", due_at=NOW - timedelta(hours=3))
    make_reminder(session, title="later today", due_at=NOW + timedelta(hours=3))
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))

    digest = due_digest(session, window="week", now=NOW)

    assert [r.title for r in digest["overdue"]] == ["late"]
    assert [r.title for r in digest["due_today"]] == ["later today"]
    assert [r.title for r in digest["upcoming"]] == ["thursday"]


def test_digest_default_window_stops_at_the_end_of_today(session):
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))
    assert due_digest(session, now=NOW)["upcoming"] == []


def test_digest_window_accepts_a_phrase(session):
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))
    assert [r.title for r in due_digest(session, window="in 4 days", now=NOW)["upcoming"]] == \
        ["thursday"]


def test_digest_excludes_resolved_reminders(session):
    make_reminder(session, title="done", due_at=NOW - timedelta(hours=3),
                  status=ReminderStatus.acked.value)
    assert due_digest(session, window="week", now=NOW)["overdue"] == []


def test_digest_day_boundary_follows_the_configured_zone(session):
    """23:00 UTC is already tomorrow in Auckland, so nothing is "today"."""
    make_reminder(session, title="soon", due_at=NOW + timedelta(hours=1))
    digest = due_digest(session, window="week", tz="Pacific/Auckland",
                        now=datetime(2026, 8, 15, 23, 0))
    assert [r.title for r in digest["upcoming"]] == ["soon"]
    assert digest["due_today"] == []
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'snooze_reminder' from 'app.service'`.

- [x] **Step 3: Implement**

Update the imports in `app/service.py`:

```python
from datetime import datetime, timedelta
# (the sqlmodel / app.errors / app.logic / app.models imports are unchanged)
from app.timeutil import (
    from_local_naive,
    parse_duration_minutes,
    parse_when,
    to_local_naive,
    to_utc_naive,
    utcnow,
)
```

and add `"snooze_reminder"`, `"expire_reminder"`, `"due_digest"` to `__all__`.

Append to `app/service.py`:

```python
def snooze_reminder(
    session: Session,
    reminder_id: int,
    *,
    duration: str | None = None,
    default_minutes: int = 15,
    max_snoozes: int = 20,
    tz: str = "UTC",
    now: datetime | None = None,
) -> Reminder:
    """Push a pending reminder's due time forward. Status stays pending.

    `duration` accepts a shorthand ("30m", "2h") or an absolute phrase
    ("tomorrow at 9am"). Omitted, it uses `default_minutes`.
    """
    now = now or utcnow()
    reminder = get_reminder(session, reminder_id)
    _require_pending(reminder)

    if reminder.snooze_count >= max_snoozes:
        raise SnoozeLimitReached(
            f"Reminder {reminder_id} has already been snoozed {reminder.snooze_count} "
            f"times (limit {max_snoozes})"
        )

    if duration is None:
        new_due = now + timedelta(minutes=default_minutes)
    else:
        minutes = parse_duration_minutes(duration)
        new_due = (
            now + timedelta(minutes=minutes)
            if minutes is not None
            else parse_when(duration, tz=tz, now=now)
        )
    if new_due <= now:
        raise InvalidTime(f"Snooze target {duration!r} is not in the future")

    reminder.due_at = new_due
    reminder.retry_count = 0
    reminder.last_sent_at = None
    reminder.snooze_count += 1
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def expire_reminder(
    session: Session, reminder: Reminder, *, now: datetime, tz: str = "UTC"
) -> None:
    """Retry budget exhausted. A recurring series rolls forward instead of dying.

    Does not commit — the scheduler resolves a whole tick in one transaction.
    """
    _resolve_occurrence(
        session,
        reminder,
        outcome=CompletionOutcome.expired.value,
        resolved_at=now,
        tz=tz,
        terminal_status=ReminderStatus.expired.value,
    )


def _end_of_local_day(now: datetime, tz: str) -> datetime:
    local = to_local_naive(now, tz)
    return from_local_naive(
        local.replace(hour=23, minute=59, second=59, microsecond=999999), tz
    )


def _resolve_horizon(window: str, *, now: datetime, tz: str, end_of_today: datetime) -> datetime:
    named = {
        "today": end_of_today,
        "tomorrow": end_of_today + timedelta(days=1),
        "week": now + timedelta(days=7),
        "all": now + timedelta(days=3650),
    }
    if window.strip().lower() in named:
        return named[window.strip().lower()]
    return parse_when(window, tz=tz, now=now)


def due_digest(
    session: Session,
    *,
    window: str = "today",
    tz: str = "UTC",
    now: datetime | None = None,
) -> dict:
    """Pending work split into overdue / due today / upcoming.

    Day boundaries follow `tz`, not UTC, so "today" means the user's today.
    """
    now = now or utcnow()
    end_of_today = _end_of_local_day(now, tz)
    horizon = _resolve_horizon(window, now=now, tz=tz, end_of_today=end_of_today)

    pending = list_reminders(session, status=ReminderStatus.pending.value)
    return {
        "now": now,
        "horizon": horizon,
        "overdue": [r for r in pending if r.due_at < now],
        "due_today": [r for r in pending if now <= r.due_at <= end_of_today],
        "upcoming": [r for r in pending if end_of_today < r.due_at <= horizon],
    }
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: all PASS.

- [x] **Step 5: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat(service): snooze, recurring expiry, and the due digest"
```

---

### Task 10: Scheduler — quiet hours and recurring expiry, wired to settings

**Files:**
- Modify: `app/scheduler.py`
- Modify: `app/main.py` (the `build_scheduler` call in `lifespan`)
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `decide()`'s quiet-hours parameters (Task 5), `expire_reminder` (Task 9).
- Produces: `tick(db, sender, *, settings, now_fn=utcnow)` — `settings` is a **required keyword**; `build_scheduler(db, sender, settings)` — the tick interval now comes from `settings.tick_seconds` rather than a separate argument.

**Note for the implementer:** every existing call site in `tests/test_scheduler.py` must gain `settings=settings` and take the `settings` fixture. That churn is deliberate — a default would let a caller silently run with the wrong timezone.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_scheduler.py`:

```python
from dataclasses import replace
from datetime import time

from app.models import Completion, CompletionOutcome

# `FakeSender`, `add`, and `load` already exist at the top of this file — reuse
# them rather than adding a parallel set of helpers.


async def test_quiet_hours_suppress_a_due_send(db, settings):
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    assert sender.sent == []


async def test_the_same_reminder_sends_once_the_window_ends(db, settings):
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    reminder_id = add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 8, 0))

    assert sender.sent == [reminder_id]


async def test_quiet_hours_never_burn_a_retry_or_expire(db, settings):
    """No send happens, so neither the retry budget nor expiry advances — a
    reminder must not be able to quietly die overnight."""
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 1, 0),
        retry_count=4,
        max_retries=4,
        last_sent_at=datetime(2026, 8, 14, 20, 0),
    )

    await tick(db, FakeSender(), settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    reminder = load(db, reminder_id)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.retry_count == 4


async def test_quiet_hours_are_evaluated_in_the_configured_zone(db, settings):
    """02:00 UTC is 04:00 in Berlin — inside a 22:00-08:00 Berlin window."""
    quiet = replace(
        settings,
        timezone="Europe/Berlin",
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(8, 0),
    )
    add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    assert sender.sent == []


async def test_expiring_a_recurring_reminder_rolls_it_forward(db, settings):
    now = datetime(2026, 8, 15, 12, 0)
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=DAILY",
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )

    await tick(db, FakeSender(), settings=settings, now_fn=lambda: now)

    reminder = load(db, reminder_id)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)
    assert reminder.retry_count == 0
    with db.session() as s:
        assert s.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


async def test_expiring_a_one_shot_reminder_is_still_terminal(db, settings):
    now = datetime(2026, 8, 15, 12, 0)
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 9, 0),
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )

    await tick(db, FakeSender(), settings=settings, now_fn=lambda: now)

    assert load(db, reminder_id).status == ReminderStatus.expired.value


async def test_a_broken_recurrence_rule_does_not_abort_the_tick(db, settings):
    """One reminder with an uncomputable rule must never stop the others from
    being processed."""
    now = datetime(2026, 8, 15, 12, 0)
    broken_id = add(
        db,
        title="broken",
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=NONSENSE",
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )
    fine_id = add(db, title="fine", due_at=datetime(2026, 8, 15, 11, 0))
    sender = FakeSender()

    await tick(db, sender, settings=settings, now_fn=lambda: now)

    assert sender.sent == [fine_id]
    assert load(db, broken_id).status == ReminderStatus.pending.value
```

**Ordering note:** `broken_id` is inserted first, so it is processed (and rolled back) before `fine` is sent — which is exactly the interleaving the rollback caution in Step 4 is about. If the assertion on `fine` ever fails, that is the rollback discarding a same-tick send, not a flake.

- [x] **Step 2: Update the existing scheduler tests**

Every existing `await tick(db, ...)` call in `tests/test_scheduler.py` gains `settings=settings`, and every test function that calls `tick` gains the `settings` fixture parameter. Every existing `build_scheduler(db, sender, N)` call becomes `build_scheduler(db, sender, replace(settings, tick_seconds=N))`.

- [x] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL with `TypeError: tick() got an unexpected keyword argument 'settings'`.

- [x] **Step 4: Implement**

In `app/scheduler.py`, update the imports:

```python
from app.config import Settings
from app.service import expire_reminder, record_send
from app.timeutil import to_local_naive, utcnow
```

Replace `tick` and `build_scheduler`:

```python
async def tick(
    db: Database,
    sender: Sender,
    *,
    settings: Settings,
    now_fn: Callable[[], datetime] = utcnow,
) -> None:
    """One scheduler pass: send what is due, expire what is spent.

    A failing send is logged and skipped without touching that reminder's
    counters, so the next tick retries it rather than burning an attempt.
    A reminder whose recurrence rule cannot be computed is likewise left
    untouched. One bad reminder never blocks the others.
    """
    now = now_fn()
    local_now = to_local_naive(now, settings.timezone)

    with db.session() as session:
        pending = session.exec(
            select(Reminder).where(Reminder.status == ReminderStatus.pending.value)
        ).all()

        for reminder in pending:
            action = decide(
                status=reminder.status,
                due_at=reminder.due_at,
                last_sent_at=reminder.last_sent_at,
                retry_count=reminder.retry_count,
                retry_interval_min=reminder.retry_interval_min,
                max_retries=reminder.max_retries,
                now=now,
                local_now=local_now,
                quiet_start=settings.quiet_hours_start,
                quiet_end=settings.quiet_hours_end,
            )

            if action is Action.SEND:
                try:
                    message_id = await sender(reminder)
                except Exception:
                    logger.exception(
                        "failed to send reminder %s (%s); will retry next tick",
                        reminder.id,
                        reminder.title,
                    )
                    continue
                record_send(session, reminder, now=now, message_id=message_id)
                logger.info(
                    "sent reminder %s (%s), attempt %s/%s",
                    reminder.id,
                    reminder.title,
                    reminder.retry_count,
                    reminder.max_retries,
                )

            elif action is Action.EXPIRE:
                try:
                    expire_reminder(session, reminder, now=now, tz=settings.timezone)
                except Exception:
                    logger.exception(
                        "could not resolve reminder %s (%s); leaving it untouched",
                        reminder.id,
                        reminder.title,
                    )
                    session.rollback()
                    continue
                logger.info(
                    "resolved reminder %s (%s) after %s attempts; now %s, due %s",
                    reminder.id,
                    reminder.title,
                    reminder.retry_count,
                    reminder.status,
                    reminder.due_at,
                )

        session.commit()


def build_scheduler(db: Database, sender: Sender, settings: Settings) -> AsyncIOScheduler:
    """An AsyncIOScheduler that runs `tick` on the app's own event loop.

    max_instances=1 plus coalesce=True mean a slow tick can never overlap
    itself or replay a backlog of missed runs — either would double-send.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger="interval",
        seconds=settings.tick_seconds,
        args=[db, sender],
        kwargs={"settings": settings},
        id="reminder-tick",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler
```

**Caution on the rollback:** `session.rollback()` in the expiry error path discards uncommitted work from earlier reminders in the same tick. That is the conservative choice — those reminders are simply re-evaluated on the next tick, whereas committing a partially-applied roll-forward would leave a series in an invalid state. Keep it.

In `app/main.py`'s `lifespan`, change the scheduler construction to:

```python
    scheduler = build_scheduler(db, sender, settings)
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v && .venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add app/scheduler.py app/main.py tests/test_scheduler.py
git commit -m "feat(scheduler): honour quiet hours and roll recurring series forward on expiry"
```

---

### Task 11: REST API — schemas, error mapping, and three new endpoints

**Files:**
- Modify: `app/schemas.py`
- Modify: `app/routers/reminders.py`
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: the whole service layer (Tasks 7–9).
- Produces:
  - Schemas: `ReminderCreate` (`due_at: str`, plus `recurrence`, `recur_from`), `ReminderUpdate` (same additions), `SnoozeRequest`, `CompletionRead`, `ConfigRead`; `ReminderRead` gains `recurrence`, `recur_from`, `snooze_count`; `ReminderDetail` gains `completions`; `to_completion_read(completion) -> CompletionRead`.
  - Endpoints: `POST /api/reminders/{id}/complete`, `POST /api/reminders/{id}/snooze`, `GET /api/config`.
  - `app/main.py`: `register_error_handlers(app)` mapping `ServiceError` subclasses to status codes.
- **Backward compatible:** every existing endpoint keeps its path, method, and response shape, so the old dashboard keeps working mid-deploy.

**Error mapping (the single table both adapters follow):**

| Exception | HTTP |
|---|---|
| `ReminderNotFound` | 404 |
| `ReminderNotPending` | 409 |
| `SnoozeLimitReached` | 409 |
| `InvalidRecurrence` | 422 |
| `InvalidTime` | 422 |
| `InvalidField` | 422 |
| any other `ServiceError` | 400 |

- [x] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_create_accepts_a_natural_language_due_at(client):
    body = create(client, due_at="in 2 hours").json()
    assert body["due_at"].endswith("+00:00")


def test_create_rejects_an_unparseable_due_at(client):
    response = create(client, due_at="sometime soonish")
    assert response.status_code == 422
    assert "sometime soonish" in response.json()["detail"]


def test_create_accepts_a_recurrence(client):
    body = create(client, recurrence="FREQ=WEEKLY;BYDAY=TU").json()
    assert body["recurrence"] == "FREQ=WEEKLY;BYDAY=TU"
    assert body["recur_from"] == "schedule"
    assert body["snooze_count"] == 0


def test_create_rejects_an_unsupported_recurrence(client):
    response = create(client, recurrence="FREQ=HOURLY")
    assert response.status_code == 422
    assert "FREQ" in response.json()["detail"]


def test_complete_marks_a_one_shot_reminder_done(client):
    reminder_id = create(client).json()["id"]
    response = client.post(f"/api/reminders/{reminder_id}/complete")
    assert response.status_code == 200
    assert response.json()["status"] == "acked"


def test_complete_rolls_a_recurring_reminder_forward(client):
    created = create(client, due_at="2026-08-12T09:00:00+00:00",
                     recurrence="FREQ=DAILY").json()
    body = client.post(f"/api/reminders/{created['id']}/complete").json()
    assert body["status"] == "pending"
    assert body["due_at"] == "2026-08-13T09:00:00+00:00"


def test_complete_of_unknown_id_is_404(client):
    assert client.post("/api/reminders/999/complete").status_code == 404


def test_complete_twice_is_409(client):
    reminder_id = create(client).json()["id"]
    client.post(f"/api/reminders/{reminder_id}/complete")
    response = client.post(f"/api/reminders/{reminder_id}/complete")
    assert response.status_code == 409
    assert "acked" in response.json()["detail"]


def test_snooze_without_a_body_uses_the_default(client):
    reminder_id = create(client).json()["id"]
    body = client.post(f"/api/reminders/{reminder_id}/snooze").json()
    assert body["status"] == "pending"
    assert body["snooze_count"] == 1


def test_snooze_accepts_a_duration(client):
    reminder_id = create(client).json()["id"]
    body = client.post(f"/api/reminders/{reminder_id}/snooze",
                       json={"duration": "2h"}).json()
    assert body["snooze_count"] == 1


def test_snooze_rejects_gibberish(client):
    reminder_id = create(client).json()["id"]
    response = client.post(f"/api/reminders/{reminder_id}/snooze",
                           json={"duration": "in a bit"})
    assert response.status_code == 422


def test_detail_includes_completions(client):
    created = create(client, due_at="2026-08-12T09:00:00+00:00",
                     recurrence="FREQ=DAILY").json()
    client.post(f"/api/reminders/{created['id']}/complete")

    completions = client.get(f"/api/reminders/{created['id']}").json()["completions"]
    assert len(completions) == 1
    assert completions[0]["outcome"] == "completed"
    assert completions[0]["scheduled_for"] == "2026-08-12T09:00:00+00:00"


def test_config_exposes_what_the_frontend_needs(client):
    body = client.get("/api/config").json()
    assert body["timezone"] == "UTC"
    assert body["default_snooze_min"] == 15
    assert body["max_snoozes"] == 20
    assert body["quiet_hours_start"] is None
    assert body["quiet_hours_end"] is None
    assert body["server_time"].endswith("+00:00")


def test_patch_refuses_to_null_a_required_field(client):
    reminder_id = create(client).json()["id"]
    response = client.patch(f"/api/reminders/{reminder_id}", json={"recur_from": None})
    assert response.status_code == 422


def test_read_schema_is_backward_compatible(client):
    """The old dashboard must keep working across the deploy."""
    body = create(client).json()
    assert {"id", "title", "note", "due_at", "retry_interval_min", "max_retries",
            "status", "retry_count", "last_sent_at", "created_at"} <= set(body)
```

Update the `create` helper at the top of `tests/test_api.py` so the new fields can be passed:

```python
def create(client, **overrides):
    payload = {
        "title": "t",
        "due_at": "2026-08-12T09:00:00+00:00",
    }
    payload.update(overrides)
    return client.post("/api/reminders", json=payload)
```

(Keep whatever extra defaults the existing helper already sets; only make sure arbitrary keys can be passed through.)

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: FAIL — 404 on `/api/config` and on the complete/snooze endpoints.

- [x] **Step 3: Extend the schemas**

In `app/schemas.py`, change `due_at` on `ReminderCreate` to a string and add the new fields:

```python
class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    # A string, not a datetime: JSON has no datetime type anyway, and this is
    # the only shape that can carry "tomorrow at 9am" as well as ISO-8601.
    due_at: str = Field(min_length=1)
    recurrence: str | None = Field(default=None, max_length=200)
    recur_from: str = Field(default="schedule")
    retry_interval_min: int = Field(default=15, ge=1, le=1440)
    max_retries: int = Field(default=4, ge=1, le=100)


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    due_at: str | None = Field(default=None, min_length=1)
    recurrence: str | None = Field(default=None, max_length=200)
    recur_from: str | None = Field(default=None)
    retry_interval_min: int | None = Field(default=None, ge=1, le=1440)
    max_retries: int | None = Field(default=None, ge=1, le=100)


class SnoozeRequest(BaseModel):
    duration: str | None = Field(default=None, max_length=100)
```

Add the new read schemas and update the existing ones:

```python
class CompletionRead(BaseModel):
    id: int
    scheduled_for: str
    completed_at: str
    outcome: str


class ConfigRead(BaseModel):
    timezone: str
    default_snooze_min: int
    max_snoozes: int
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    server_time: str
```

Add to `ReminderRead` (after `created_at`):

```python
    recurrence: str | None
    recur_from: str
    snooze_count: int
```

Add to `ReminderDetail`:

```python
    completions: list[CompletionRead]
```

Update the converters:

```python
def to_completion_read(completion: Completion) -> CompletionRead:
    return CompletionRead(
        id=completion.id,
        scheduled_for=as_utc_iso(completion.scheduled_for),
        completed_at=as_utc_iso(completion.completed_at),
        outcome=completion.outcome,
    )


def to_read(reminder: Reminder) -> ReminderRead:
    return ReminderRead(
        id=reminder.id,
        title=reminder.title,
        note=reminder.note,
        due_at=as_utc_iso(reminder.due_at),
        retry_interval_min=reminder.retry_interval_min,
        max_retries=reminder.max_retries,
        status=reminder.status,
        retry_count=reminder.retry_count,
        last_sent_at=as_utc_iso(reminder.last_sent_at),
        created_at=as_utc_iso(reminder.created_at),
        recurrence=reminder.recurrence,
        recur_from=reminder.recur_from,
        snooze_count=reminder.snooze_count,
    )


def to_detail(
    reminder: Reminder,
    notifications: list[Notification],
    completions: list[Completion],
) -> ReminderDetail:
    return ReminderDetail(
        **to_read(reminder).model_dump(),
        notifications=[to_notification_read(n) for n in notifications],
        completions=[to_completion_read(c) for c in completions],
    )
```

Add `Completion` to the `app.models` import at the top of the file.

**Times stay UTC ISO in the API.** The browser converts using the zone from `/api/config`; putting local times in the payload would make the API's meaning depend on server configuration.

- [x] **Step 4: Rewrite the router as a thin adapter**

Replace `app/routers/reminders.py` in full:

```python
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from app import service
from app.config import Settings
from app.db import Database
from app.models import Completion, Notification, ReminderStatus
from app.schemas import (
    ConfigRead,
    ReminderCreate,
    ReminderDetail,
    ReminderRead,
    ReminderUpdate,
    SnoozeRequest,
    to_detail,
    to_read,
)
from app.timeutil import as_local_iso, utcnow

router = APIRouter(prefix="/api", tags=["reminders"])


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.db
    with database.session() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config", response_model=ConfigRead)
def read_config(settings: Settings = Depends(get_settings)):
    """Everything the frontend needs to render times and label its controls."""
    return ConfigRead(
        timezone=settings.timezone,
        default_snooze_min=settings.default_snooze_min,
        max_snoozes=settings.max_snoozes,
        quiet_hours_start=(
            settings.quiet_hours_start.strftime("%H:%M")
            if settings.quiet_hours_start else None
        ),
        quiet_hours_end=(
            settings.quiet_hours_end.strftime("%H:%M")
            if settings.quiet_hours_end else None
        ),
        server_time=as_local_iso(utcnow(), settings.timezone),
    )


@router.post("/reminders", response_model=ReminderRead, status_code=201)
def create_reminder(
    payload: ReminderCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.create_reminder(
            session,
            title=payload.title,
            note=payload.note,
            due_at=payload.due_at,
            recurrence=payload.recurrence,
            recur_from=payload.recur_from,
            retry_interval_min=payload.retry_interval_min,
            max_retries=payload.max_retries,
            tz=settings.timezone,
        )
    )


@router.get("/reminders", response_model=list[ReminderRead])
def list_reminders(
    status: ReminderStatus | None = None,
    session: Session = Depends(get_session),
):
    reminders = service.list_reminders(
        session, status=status.value if status is not None else None
    )
    return [to_read(r) for r in reminders]


@router.get("/reminders/{reminder_id}", response_model=ReminderDetail)
def get_reminder(reminder_id: int, session: Session = Depends(get_session)):
    reminder = service.get_reminder(session, reminder_id)
    notifications = session.exec(
        select(Notification)
        .where(Notification.reminder_id == reminder_id)
        .order_by(Notification.sent_at, Notification.id)
    ).all()
    completions = session.exec(
        select(Completion)
        .where(Completion.reminder_id == reminder_id)
        .order_by(Completion.completed_at, Completion.id)
    ).all()
    return to_detail(reminder, list(notifications), list(completions))


@router.patch("/reminders/{reminder_id}", response_model=ReminderRead)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.update_reminder(
            session,
            reminder_id,
            payload.model_dump(exclude_unset=True),
            tz=settings.timezone,
        )
    )


@router.post("/reminders/{reminder_id}/complete", response_model=ReminderRead)
def complete_reminder(
    reminder_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.complete_reminder(session, reminder_id, tz=settings.timezone)
    )


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderRead)
def snooze_reminder(
    reminder_id: int,
    payload: SnoozeRequest | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.snooze_reminder(
            session,
            reminder_id,
            duration=payload.duration if payload else None,
            default_minutes=settings.default_snooze_min,
            max_snoozes=settings.max_snoozes,
            tz=settings.timezone,
        )
    )


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: int, session: Session = Depends(get_session)):
    service.delete_reminder(session, reminder_id)
    return Response(status_code=204)
```

- [x] **Step 5: Register the error handlers**

Add to `app/main.py`:

```python
from fastapi.responses import JSONResponse

from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
    ServiceError,
    SnoozeLimitReached,
)

# The single mapping from domain error to HTTP status. Starlette resolves a
# handler by walking the exception's MRO, so registering ServiceError once
# covers every subclass, including ones added later.
ERROR_STATUS = {
    ReminderNotFound: 404,
    ReminderNotPending: 409,
    SnoozeLimitReached: 409,
    InvalidRecurrence: 422,
    InvalidTime: 422,
    InvalidField: 422,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def handle_service_error(request, exc: ServiceError):
        return JSONResponse(
            status_code=ERROR_STATUS.get(type(exc), 400),
            content={"detail": str(exc)},
        )
```

and call it in `create_app()`, before `include_router`:

```python
    register_error_handlers(app)
    app.include_router(reminders.router)
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: all PASS — the new tests **and** every pre-existing one, unchanged.

- [x] **Step 7: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 8: Commit**

```bash
git add app/schemas.py app/routers/reminders.py app/main.py tests/test_api.py
git commit -m "feat(api): complete/snooze/config endpoints on the service layer"
```

---

### Task 12: MCP server — the nine tools

**Files:**
- Create: `app/mcp_server.py`
- Create: `tests/test_mcp.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: the whole service layer.
- Produces: `build_mcp(db: Database, settings: Settings) -> MCPServer` with tools `create_reminder`, `list_reminders`, `get_reminder`, `update_reminder`, `complete_reminder`, `snooze_reminder`, `delete_reminder`, `search_reminders`, `whats_due`.
- Every tool result is a dict containing `timezone` and `server_time` (fresh local ISO) alongside its payload. `due_at`, `last_sent_at`, and completion times are rendered as **local ISO with offset** — absolute and unambiguous, and readable in the terms the user thinks in.
- This module is the single MCP entry point and therefore the future auth seam (spec §4). Do not let MCP concerns leak into `service.py`.

- [x] **Step 1: Pin the dependency**

Append to `requirements.txt`:

```
mcp>=2.0,<3.0
```

Run: `.venv/bin/pip install -r requirements.txt`
Expected: already satisfied (`mcp==2.0.0`).

- [x] **Step 2: Write the failing tests**

Create `tests/test_mcp.py`:

```python
import json
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp_server import build_mcp
from app.models import Reminder, ReminderStatus


@pytest.fixture
def mcp(db, settings):
    return build_mcp(db, settings)


async def call(mcp, name, **args) -> dict:
    """MCP 2.0 returns a plain dict as JSON text with structured_content=None."""
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


def seed(db, **overrides) -> int:
    fields = dict(title="t", due_at=datetime(2026, 8, 15, 9, 0))
    fields.update(overrides)
    with db.session() as session:
        reminder = Reminder(**fields)
        session.add(reminder)
        session.commit()
        session.refresh(reminder)
        return reminder.id


async def test_every_spec_tool_is_registered(mcp):
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "create_reminder", "list_reminders", "get_reminder", "update_reminder",
        "complete_reminder", "snooze_reminder", "delete_reminder",
        "search_reminders", "whats_due",
    }


async def test_tool_descriptions_name_the_configured_timezone(mcp):
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "UTC" in tools["create_reminder"].description


async def test_every_result_carries_the_timezone_and_a_fresh_server_time(mcp):
    body = await call(mcp, "list_reminders")
    assert body["timezone"] == "UTC"
    assert body["server_time"].endswith("+00:00")


# --- create ---------------------------------------------------------------

async def test_create_echoes_the_resolved_absolute_time(mcp):
    """A misparse must be visible immediately, not days later as a missed
    reminder."""
    body = await call(mcp, "create_reminder", title="bins", due_at="in 2 hours")
    assert body["reminder"]["title"] == "bins"
    assert body["reminder"]["due_at"].endswith("+00:00")


async def test_create_accepts_iso(mcp):
    body = await call(mcp, "create_reminder", title="t",
                      due_at="2026-08-15T09:00:00+00:00")
    assert body["reminder"]["due_at"] == "2026-08-15T09:00:00+00:00"


async def test_create_accepts_a_recurrence(mcp):
    body = await call(mcp, "create_reminder", title="bins",
                      due_at="2026-08-15T09:00:00+00:00",
                      recurrence="FREQ=WEEKLY;BYDAY=TU")
    assert body["reminder"]["recurrence"] == "FREQ=WEEKLY;BYDAY=TU"


async def test_create_reports_an_unparseable_date_actionably(mcp):
    with pytest.raises(ToolError, match="sometime soonish"):
        await call(mcp, "create_reminder", title="t", due_at="sometime soonish")


async def test_create_reports_an_unsupported_recurrence_actionably(mcp):
    with pytest.raises(ToolError, match="FREQ=HOURLY is not supported"):
        await call(mcp, "create_reminder", title="t",
                   due_at="2026-08-15T09:00:00+00:00", recurrence="FREQ=HOURLY")


# --- read -----------------------------------------------------------------

async def test_list_defaults_to_pending(mcp, db):
    seed(db, title="open")
    seed(db, title="closed", status=ReminderStatus.acked.value)
    body = await call(mcp, "list_reminders")
    assert [r["title"] for r in body["reminders"]] == ["open"]


async def test_list_can_ask_for_another_status(mcp, db):
    seed(db, title="closed", status=ReminderStatus.acked.value)
    body = await call(mcp, "list_reminders", status="acked")
    assert [r["title"] for r in body["reminders"]] == ["closed"]


async def test_list_honours_limit(mcp, db):
    seed(db, title="a")
    seed(db, title="b")
    assert len((await call(mcp, "list_reminders", limit=1))["reminders"]) == 1


async def test_get_includes_notification_history(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "get_reminder", reminder_id=reminder_id)
    assert body["reminder"]["id"] == reminder_id
    assert body["notifications"] == []
    assert body["completions"] == []


async def test_get_of_unknown_id_is_actionable(mcp):
    with pytest.raises(ToolError, match="999 not found"):
        await call(mcp, "get_reminder", reminder_id=999)


async def test_search_matches_title_and_note(mcp, db):
    seed(db, title="take the bins out")
    seed(db, title="call mum", note="about the bins")
    seed(db, title="unrelated")
    body = await call(mcp, "search_reminders", query="bins")
    assert len(body["reminders"]) == 2


# --- mutate ---------------------------------------------------------------

async def test_update_changes_only_what_was_given(mcp, db):
    reminder_id = seed(db, title="old", note="keep")
    body = await call(mcp, "update_reminder", reminder_id=reminder_id, title="new")
    assert body["reminder"]["title"] == "new"
    assert body["reminder"]["note"] == "keep"


async def test_update_can_clear_a_recurrence(mcp, db):
    reminder_id = seed(db, recurrence="FREQ=DAILY")
    body = await call(mcp, "update_reminder", reminder_id=reminder_id,
                      clear_recurrence=True)
    assert body["reminder"]["recurrence"] is None


async def test_update_of_a_resolved_reminder_is_actionable(mcp, db):
    reminder_id = seed(db, status=ReminderStatus.acked.value)
    with pytest.raises(ToolError, match="already acked"):
        await call(mcp, "update_reminder", reminder_id=reminder_id, title="x")


async def test_complete_resolves_a_one_shot_reminder(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "complete_reminder", reminder_id=reminder_id)
    assert body["reminder"]["status"] == "acked"


async def test_complete_rolls_a_recurring_series_forward(mcp, db):
    reminder_id = seed(db, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY")
    body = await call(mcp, "complete_reminder", reminder_id=reminder_id)
    assert body["reminder"]["status"] == "pending"
    assert body["reminder"]["due_at"].startswith("2026-08-16T09:00")


async def test_complete_twice_is_actionable(mcp, db):
    reminder_id = seed(db)
    await call(mcp, "complete_reminder", reminder_id=reminder_id)
    with pytest.raises(ToolError, match="already acked"):
        await call(mcp, "complete_reminder", reminder_id=reminder_id)


async def test_snooze_uses_the_configured_default(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "snooze_reminder", reminder_id=reminder_id)
    assert body["reminder"]["snooze_count"] == 1


async def test_snooze_accepts_a_natural_duration(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "snooze_reminder", reminder_id=reminder_id, duration="2h")
    assert body["reminder"]["snooze_count"] == 1


async def test_snooze_reports_an_unreadable_duration(mcp, db):
    reminder_id = seed(db)
    with pytest.raises(ToolError):
        await call(mcp, "snooze_reminder", reminder_id=reminder_id, duration="in a bit")


async def test_delete_removes_the_reminder(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "delete_reminder", reminder_id=reminder_id)
    assert body["deleted"] == reminder_id
    with pytest.raises(ToolError, match="not found"):
        await call(mcp, "get_reminder", reminder_id=reminder_id)


async def test_delete_of_unknown_id_is_actionable(mcp):
    with pytest.raises(ToolError, match="not found"):
        await call(mcp, "delete_reminder", reminder_id=999)


# --- digest ---------------------------------------------------------------

async def test_whats_due_buckets_the_work(mcp, db):
    from app.timeutil import utcnow
    now = utcnow()
    seed(db, title="late", due_at=now - timedelta(hours=2))
    seed(db, title="next week", due_at=now + timedelta(days=6))

    body = await call(mcp, "whats_due", window="week")

    assert [r["title"] for r in body["overdue"]] == ["late"]
    assert [r["title"] for r in body["upcoming"]] == ["next week"]


async def test_whats_due_defaults_to_today(mcp, db):
    from app.timeutil import utcnow
    seed(db, title="next week", due_at=utcnow() + timedelta(days=6))
    assert (await call(mcp, "whats_due"))["upcoming"] == []
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server'`.

- [x] **Step 4: Implement the MCP server**

Create `app/mcp_server.py`:

```python
"""The MCP connector.

Every MCP request enters through this module and nothing else, which is what
keeps a future Authorization-header check to one function rather than a
scattering of edits (spec §4). Business rules live in app/service.py; this
file only translates between MCP tool calls and that layer.
"""

from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlmodel import select

from app import service
from app.config import Settings
from app.db import Database
from app.errors import ServiceError
from app.models import Completion, Notification, Reminder
from app.timeutil import as_local_iso, utcnow


@contextmanager
def _work(db: Database):
    """A session plus domain-error translation, in one place.

    Domain errors carry an actionable message ("reminder 12 is already acked
    and cannot be edited"); re-raising them as ToolError is what puts that
    message in front of the model instead of a stack trace.
    """
    try:
        with db.session() as session:
            yield session
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


def _reminder_dict(reminder: Reminder, tz: str) -> dict:
    """Absolute times, rendered in the configured zone with an explicit offset.

    Unambiguous to a machine and readable in the terms the user thinks in.
    """
    return {
        "id": reminder.id,
        "title": reminder.title,
        "note": reminder.note,
        "due_at": as_local_iso(reminder.due_at, tz),
        "status": reminder.status,
        "recurrence": reminder.recurrence,
        "recur_from": reminder.recur_from,
        "snooze_count": reminder.snooze_count,
        "retry_count": reminder.retry_count,
        "max_retries": reminder.max_retries,
        "retry_interval_min": reminder.retry_interval_min,
        "last_sent_at": as_local_iso(reminder.last_sent_at, tz),
        "created_at": as_local_iso(reminder.created_at, tz),
    }


def build_mcp(db: Database, settings: Settings) -> MCPServer:
    tz = settings.timezone

    def envelope(**payload) -> dict:
        """Every result carries the zone and a *fresh* local clock reading.

        The timezone also appears in each tool description, but the current
        time deliberately does not: a description is built once at startup and
        an embedded clock would be stale — and actively misleading — within
        minutes.
        """
        return {"timezone": tz, "server_time": as_local_iso(utcnow(), tz), **payload}

    def one(reminder: Reminder) -> dict:
        return envelope(reminder=_reminder_dict(reminder, tz))

    def many(reminders: list[Reminder], **extra) -> dict:
        return envelope(reminders=[_reminder_dict(r, tz) for r in reminders], **extra)

    mcp = MCPServer(
        name="reminders",
        instructions=(
            f"Reminder service. All times are in {tz}. Relative phrasings like "
            "'tomorrow at 9am' or 'in 2 hours' are resolved server-side in that "
            "zone; every result echoes the resolved absolute time, so check it "
            "before confirming to the user."
        ),
    )

    when_help = (
        f"ISO-8601 or natural language ('tomorrow at 9am', 'in 2 hours', "
        f"'friday 18:00'), resolved in {tz}."
    )
    recurrence_help = (
        "Optional RRULE subset: FREQ=DAILY|WEEKLY|MONTHLY|YEARLY, optional "
        "INTERVAL=<n>, and BYDAY=MO,TU,... for weekly rules only. "
        "Anything else is rejected."
    )

    @mcp.tool(description=f"Create a reminder. due_at: {when_help} {recurrence_help}")
    def create_reminder(
        title: str,
        due_at: str,
        note: str | None = None,
        recurrence: str | None = None,
        recur_from: str = "schedule",
        retry_interval_min: int = 15,
        max_retries: int = 4,
    ) -> dict:
        with _work(db) as session:
            return one(
                service.create_reminder(
                    session,
                    title=title,
                    due_at=due_at,
                    note=note,
                    recurrence=recurrence,
                    recur_from=recur_from,
                    retry_interval_min=retry_interval_min,
                    max_retries=max_retries,
                    tz=tz,
                )
            )

    @mcp.tool(
        description=f"List reminders, pending by default. Times are in {tz}."
    )
    def list_reminders(status: str = "pending", limit: int = 50) -> dict:
        with _work(db) as session:
            return many(service.list_reminders(session, status=status, limit=limit))

    @mcp.tool(
        description=f"One reminder with its notification and completion history. Times are in {tz}."
    )
    def get_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            reminder = service.get_reminder(session, reminder_id)
            notifications = session.exec(
                select(Notification)
                .where(Notification.reminder_id == reminder_id)
                .order_by(Notification.sent_at, Notification.id)
            ).all()
            completions = session.exec(
                select(Completion)
                .where(Completion.reminder_id == reminder_id)
                .order_by(Completion.completed_at, Completion.id)
            ).all()
            return envelope(
                reminder=_reminder_dict(reminder, tz),
                notifications=[
                    {
                        "id": n.id,
                        "sent_at": as_local_iso(n.sent_at, tz),
                        "acked_at": as_local_iso(n.acked_at, tz),
                    }
                    for n in notifications
                ],
                completions=[
                    {
                        "id": c.id,
                        "scheduled_for": as_local_iso(c.scheduled_for, tz),
                        "completed_at": as_local_iso(c.completed_at, tz),
                        "outcome": c.outcome,
                    }
                    for c in completions
                ],
            )

    @mcp.tool(
        description=(
            f"Edit a pending reminder; only the fields you pass change. "
            f"due_at: {when_help} Pass clear_recurrence=true to turn a "
            f"repeating reminder back into a one-shot."
        )
    )
    def update_reminder(
        reminder_id: int,
        title: str | None = None,
        note: str | None = None,
        due_at: str | None = None,
        recurrence: str | None = None,
        recur_from: str | None = None,
        retry_interval_min: int | None = None,
        max_retries: int | None = None,
        clear_recurrence: bool = False,
    ) -> dict:
        changes = {
            key: value
            for key, value in {
                "title": title,
                "note": note,
                "due_at": due_at,
                "recurrence": recurrence,
                "recur_from": recur_from,
                "retry_interval_min": retry_interval_min,
                "max_retries": max_retries,
            }.items()
            if value is not None
        }
        if clear_recurrence:
            # An omitted argument and an explicit null are indistinguishable
            # over JSON-RPC, so clearing needs its own flag.
            changes["recurrence"] = None

        with _work(db) as session:
            return one(service.update_reminder(session, reminder_id, changes, tz=tz))

    @mcp.tool(
        description=(
            "Mark a reminder done. A repeating reminder rolls forward to its "
            "next occurrence instead of closing."
        )
    )
    def complete_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            return one(service.complete_reminder(session, reminder_id, tz=tz))

    @mcp.tool(
        description=(
            f"Push a reminder later. duration accepts '30m', '2h', or a phrase "
            f"like 'tomorrow at 9am' resolved in {tz}. Omit it to use the "
            f"configured default of {settings.default_snooze_min} minutes."
        )
    )
    def snooze_reminder(reminder_id: int, duration: str | None = None) -> dict:
        with _work(db) as session:
            return one(
                service.snooze_reminder(
                    session,
                    reminder_id,
                    duration=duration,
                    default_minutes=settings.default_snooze_min,
                    max_snoozes=settings.max_snoozes,
                    tz=tz,
                )
            )

    @mcp.tool(description="Delete a reminder permanently, with its history.")
    def delete_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            service.delete_reminder(session, reminder_id)
            return envelope(deleted=reminder_id)

    @mcp.tool(description="Substring search over reminder titles and notes.")
    def search_reminders(query: str, status: str | None = None) -> dict:
        with _work(db) as session:
            return many(service.search_reminders(session, query, status=status))

    @mcp.tool(
        description=(
            f"What needs attention, split into overdue / due today / upcoming. "
            f"window accepts 'today' (default), 'tomorrow', 'week', 'all', or a "
            f"phrase like 'in 3 days'. Day boundaries are in {tz}."
        )
    )
    def whats_due(window: str = "today") -> dict:
        with _work(db) as session:
            digest = service.due_digest(session, window=window, tz=tz)
            return envelope(
                horizon=as_local_iso(digest["horizon"], tz),
                overdue=[_reminder_dict(r, tz) for r in digest["overdue"]],
                due_today=[_reminder_dict(r, tz) for r in digest["due_today"]],
                upcoming=[_reminder_dict(r, tz) for r in digest["upcoming"]],
            )

    return mcp
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp.py -v`
Expected: all PASS. No HTTP is involved — `await mcp.call_tool(...)` exercises the tools directly.

- [x] **Step 6: Verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 7: Commit**

```bash
git add app/mcp_server.py tests/test_mcp.py requirements.txt
git commit -m "feat(mcp): nine reminder tools over the service layer"
```

---

### Task 13: Mount `/mcp` into the FastAPI app

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `build_mcp` (Task 12), `settings.mcp_enabled` (Task 1).
- Produces: `POST /mcp` speaking Streamable HTTP; `app.state.mcp` holding the `MCPServer` or `None`.

**The three things that will bite you** (all verified — do not experiment):

1. `mcp.streamable_http_app(...)` must be called before `mcp.session_manager` is read; it constructs the manager. It is called purely for that side effect here.
2. `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` is **required**, or every request — including from `TestClient` and including the public Funnel — returns 421 "Invalid Host header".
3. Register with `app.router.routes.append(Route("/mcp", endpoint=StreamableHTTPASGIApp(...)))`, **not** `app.mount("/mcp", ...)`. A `Mount` never matches the bare prefix and would 307-redirect `/mcp` to `/mcp/`. Omit `methods=` so every verb reaches the ASGI app. It must be registered before the `StaticFiles` mount.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
from dataclasses import replace

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def test_mcp_answers_on_the_bare_path_without_redirecting(client):
    """A Mount would 307 /mcp -> /mcp/; connector URLs must not depend on a
    trailing slash."""
    response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 200
    assert "serverInfo" in response.text


def test_mcp_does_not_shadow_the_api(client):
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_mcp_does_not_shadow_static_files(client):
    assert client.get("/").status_code == 200


def test_mcp_can_be_disabled_by_configuration(db, monkeypatch):
    """The escape hatch: drop the sub-app without a code change."""
    monkeypatch.setenv("MCP_ENABLED", "false")
    with TestClient(create_app(db=db)) as disabled_client:
        assert disabled_client.app.state.mcp is None
        # StaticFiles owns "/" and answers everything else with its own 404.
        assert disabled_client.post("/mcp", json=INITIALIZE,
                                    headers=MCP_HEADERS).status_code != 200
```

Check the existing imports at the top of `tests/test_main.py` and add `TestClient` / `create_app` only if they are not already there.

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: FAIL — `/mcp` returns 404 from StaticFiles.

- [x] **Step 3: Implement the mount**

Add to `app/main.py`'s imports:

```python
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app.mcp_server import build_mcp
```

Add this helper above `create_app`:

```python
def mount_mcp(app: FastAPI) -> None:
    """Register the MCP connector at exactly /mcp.

    streamable_http_app() is called only for its side effect: it constructs
    mcp.session_manager, which the ASGI app below needs. DNS-rebinding
    protection must be off or every request — TestClient and Funnel alike —
    is rejected with 421 for an "invalid" Host header.

    A Route, not a Mount: Mount never matches its bare prefix, so /mcp would
    307 to /mcp/ and the connector URL would depend on a trailing slash.
    """
    mcp = build_mcp(app.state.db, app.state.settings)
    mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.state.mcp = mcp
    app.router.routes.append(
        Route("/mcp", endpoint=StreamableHTTPASGIApp(mcp.session_manager))
    )
```

In `create_app()`, between `register_error_handlers(app)`/`include_router` and the `StaticFiles` mount:

```python
    app.include_router(reminders.router)
    if settings.mcp_enabled:
        mount_mcp(app)
    else:
        app.state.mcp = None
        logger.warning("MCP_ENABLED is false — the /mcp connector is not mounted")
    # Mounted last: StaticFiles owns "/" and would otherwise shadow /api and /mcp.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
```

In `lifespan`, wrap the existing `yield` so the session manager runs for the app's lifetime. FastMCP's own lifespan is never invoked for a mounted sub-app, so without this every `initialize` hangs or 500s:

```python
    try:
        if app.state.mcp is not None:
            async with app.state.mcp.session_manager.run():
                logger.info("MCP connector mounted at /mcp")
                yield
        else:
            yield
    finally:
        scheduler.shutdown(wait=False)
        # (the rest of the existing finally block is unchanged)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: all PASS.

- [x] **Step 5: Verify the whole suite and the real server**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

Then verify against a real uvicorn process rather than `TestClient`:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799 &
sleep 3
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8799/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
kill %1
```

Expected: `200`. A `307` means a `Mount` slipped in; a `421` means the transport-security setting is missing.

- [x] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat(mcp): mount the connector at /mcp with a lifespan-run session manager"
```

---

### Task 14: Telegram — a snooze button beside Done

**Files:**
- Modify: `app/bot.py`
- Modify: `app/main.py` (the `sender` partial and `build_application` call)
- Modify: `tests/test_bot.py`

**Interfaces:**
- Consumes: `settings.default_snooze_min`, `settings.timezone`, `service.snooze_reminder`.
- Produces: `SNOOZE_PREFIX = "snooze:"`; `send_reminder_message(bot, chat_id, reminder, *, tz="UTC", snooze_min=15) -> int`; `handle_callback(update, context, *, db, settings)` and `handle_text(update, context, *, db, settings)` — **`chat_id` now comes from `settings.chat_id`**, so the handlers take one fewer argument; `build_application(settings, db) -> Application`.
- Unchanged on purpose: dual-ack behaviour (inline button *and* bare text reply), and `find_reply_ack_target`'s "most recently nagged pending reminder" semantics.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_bot.py`:

```python
from dataclasses import replace

from app.bot import SNOOZE_PREFIX


async def test_send_offers_both_done_and_snooze():
    bot = FakeBot()
    reminder = Reminder(id=7, title="t", due_at=datetime(2026, 8, 15, 9, 0))
    await send_reminder_message(bot, CHAT_ID, reminder, snooze_min=15)

    buttons = bot.sent[-1]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["ack:7", "snooze:7"]
    assert "15" in buttons[1].text


async def test_send_shows_the_recurrence_in_the_body():
    bot = FakeBot()
    reminder = Reminder(id=1, title="bins", due_at=datetime(2026, 8, 15, 9, 0),
                        recurrence="FREQ=WEEKLY;BYDAY=TU")
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert "FREQ=WEEKLY;BYDAY=TU" in bot.sent[-1]["text"]


async def test_send_omits_the_recurrence_line_for_a_one_shot():
    bot = FakeBot()
    reminder = Reminder(id=1, title="t", due_at=datetime(2026, 8, 15, 9, 0))
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert "Repeats" not in bot.sent[-1]["text"]


async def test_snooze_button_pushes_the_reminder_out(db, settings):
    reminder_id = add_pending(db)
    update = fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}")

    await handle_callback(update, fake_context(), db=db, settings=settings)

    with db.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == ReminderStatus.pending.value
        assert reminder.snooze_count == 1


async def test_snooze_button_uses_the_configured_default(db, settings):
    reminder_id = add_pending(db)
    before = utcnow()

    await handle_callback(
        fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}"),
        fake_context(), db=db, settings=replace(settings, default_snooze_min=45),
    )

    with db.session() as session:
        due = session.get(Reminder, reminder_id).due_at
    assert timedelta(minutes=44) < due - before < timedelta(minutes=46)


async def test_snooze_beyond_the_cap_is_reported_not_crashed(db, settings):
    reminder_id = add_pending(db, snooze_count=2)
    update = fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}")

    await handle_callback(update, fake_context(), db=db,
                          settings=replace(settings, max_snoozes=2))

    with db.session() as session:
        assert session.get(Reminder, reminder_id).snooze_count == 2


async def test_done_button_on_a_recurring_reminder_rolls_it_forward(db, settings):
    reminder_id = add_pending(db, due_at=datetime(2026, 8, 15, 9, 0),
                              recurrence="FREQ=DAILY")

    await handle_callback(fake_callback_update(f"ack:{reminder_id}"),
                          fake_context(), db=db, settings=settings)

    with db.session() as session:
        assert session.get(Reminder, reminder_id).status == ReminderStatus.pending.value
```

Reuse whatever `FakeBot`, `fake_callback_update`, `fake_context`, `fake_text_update`, and reminder-seeding helpers already exist in `tests/test_bot.py`; add `add_pending(db, **overrides)` only if there is no equivalent. Add the imports the new tests need (`timedelta`, `ReminderStatus`, `utcnow`).

- [x] **Step 2: Update the existing bot tests for the new handler signature**

In every existing call, replace `db=db, chat_id=CHAT_ID` with `db=db, settings=settings` and add the `settings` fixture to the test's parameters. Where a test needs the authorised chat id to be `CHAT_ID`, build it as `replace(settings, chat_id=CHAT_ID)`; the simplest way is a module-level fixture:

```python
@pytest.fixture
def settings(settings):
    return replace(settings, chat_id=CHAT_ID, bot_token="123:abc")
```

Update `test_send_builds_a_message_with_a_done_button` — it now sees two buttons, so assert on `buttons[0]` rather than on the row's length.

- [x] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot.py -v`
Expected: FAIL with `ImportError: cannot import name 'SNOOZE_PREFIX' from 'app.bot'`.

- [x] **Step 4: Implement**

In `app/bot.py`, update the imports:

```python
from app.config import Settings
from app.db import Database
from app.errors import ServiceError
from app.models import Reminder
from app.service import ack_reminder, find_reply_ack_target, latest_notification, snooze_reminder
from app.timeutil import as_local_iso, to_local_naive, utcnow
```

Add beside `CALLBACK_PREFIX`:

```python
SNOOZE_PREFIX = "snooze:"
```

Replace `_compose` and `send_reminder_message`:

```python
def _compose(reminder: Reminder, tz: str) -> str:
    """Plain-text message body. No parse_mode, so titles never need escaping."""
    lines = [f"⏰ {reminder.title}"]
    if reminder.note:
        lines.append(reminder.note)
    if reminder.recurrence:
        lines.append(f"Repeats: {reminder.recurrence}")
    lines.append(
        f"Due {as_local_iso(reminder.due_at, tz)} · "
        f"attempt {reminder.retry_count + 1}/{reminder.max_retries}"
    )
    return "\n\n".join(lines)


async def send_reminder_message(
    bot, chat_id: int, reminder: Reminder, *, tz: str = "UTC", snooze_min: int = 15
) -> int:
    """Send one nag with Done and Snooze buttons. Returns the message id."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"{CALLBACK_PREFIX}{reminder.id}"),
        InlineKeyboardButton(
            f"💤 Snooze {snooze_min}m", callback_data=f"{SNOOZE_PREFIX}{reminder.id}"
        ),
    ]])
    message = await bot.send_message(
        chat_id=chat_id,
        text=_compose(reminder, tz),
        reply_markup=keyboard,
    )
    return message.message_id
```

Replace `handle_callback`:

```python
async def handle_callback(update, context, *, db: Database, settings: Settings) -> None:
    """Inline button tap — either '✅ Done' or '💤 Snooze'."""
    query = update.callback_query
    chat_id = settings.chat_id
    if query.message.chat_id != chat_id:
        logger.warning("ignoring callback from unauthorised chat %s", query.message.chat_id)
        await query.answer("Not authorised.")
        return

    data = query.data or ""
    for prefix in (CALLBACK_PREFIX, SNOOZE_PREFIX):
        if data.startswith(prefix):
            break
    else:
        await query.answer()
        return

    try:
        reminder_id = int(data[len(prefix):])
    except ValueError:
        await query.answer()
        return

    await query.answer()
    now = utcnow()
    original = query.message.text or ""

    if prefix == SNOOZE_PREFIX:
        with db.session() as session:
            try:
                reminder = snooze_reminder(
                    session,
                    reminder_id,
                    default_minutes=settings.default_snooze_min,
                    max_snoozes=settings.max_snoozes,
                    tz=settings.timezone,
                    now=now,
                )
            except ServiceError as exc:
                # A cap or a stale button is normal user behaviour, not a bug.
                await query.edit_message_text(text=f"{original}\n\n💤 {exc}")
                return
            local_due = to_local_naive(reminder.due_at, settings.timezone)
        await query.edit_message_text(text=f"{original}\n\n💤 Snoozed until {local_due:%H:%M}")
        return

    with db.session() as session:
        acked = ack_reminder(session, reminder_id, now=now, tz=settings.timezone)

    local_now = to_local_naive(now, settings.timezone)
    suffix = f"✅ Done at {local_now:%H:%M}" if acked else "(already resolved)"
    # Passing no reply_markup drops the buttons, so the message cannot be re-tapped.
    await query.edit_message_text(text=f"{original}\n\n{suffix}")
```

In `handle_text`, replace the `chat_id` parameter with `settings` and derive it:

```python
async def handle_text(update, context, *, db: Database, settings: Settings) -> None:
    """Any plain-text reply counts as an ack (spec §5) — no intent parsing."""
    chat_id = settings.chat_id
    if update.effective_chat.id != chat_id:
        logger.warning("ignoring message from unauthorised chat %s", update.effective_chat.id)
        return

    now = utcnow()
    with db.session() as session:
        target = find_reply_ack_target(session)
        if target is None:
            await update.message.reply_text("Nothing pending.")
            return
        title = target.title
        notification = latest_notification(session, target.id)
        message_id = notification.telegram_message_id if notification else None
        ack_reminder(session, target.id, now=now, tz=settings.timezone)

    await update.message.reply_text(f"✅ Marked “{title}” done.")

    if message_id is not None:
        local_now = to_local_naive(now, settings.timezone)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏰ {title}\n\n✅ Done at {local_now:%H:%M}",
            )
        except Exception:
            # The original may be too old to edit; the ack itself already stuck.
            logger.info("could not edit message %s for reminder ack", message_id)
```

Replace `build_application`:

```python
def build_application(settings: Settings, db: Database) -> Application:
    """Wire the long-polling Telegram application.

    The chat filter is a second guard in front of the per-handler check, so an
    unauthorised chat is dropped before any handler body runs.
    """
    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback, db=db, settings=settings))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=settings.chat_id),
            partial(handle_text, db=db, settings=settings),
        )
    )
    return application
```

In `app/main.py`'s `lifespan`, update the two call sites:

```python
        telegram_app = build_application(settings, db)
        # (the initialize / start / start_polling lines are unchanged)
        sender = partial(
            send_reminder_message,
            telegram_app.bot,
            settings.chat_id,
            tz=settings.timezone,
            snooze_min=settings.default_snooze_min,
        )
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bot.py -v && .venv/bin/python -m pytest -q`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add app/bot.py app/main.py tests/test_bot.py
git commit -m "feat(telegram): snooze button, recurrence in the body, local times"
```

---

### Task 15: Dashboard shell — markup and styles

**Files:**
- Create: `static/style.css`
- Modify: `static/index.html`

**Interfaces:**
- Produces the DOM contract the next two tasks script against. Element ids and classes here are **load-bearing** — Tasks 16 and 17 reference them by name:
  - `#create-form` with fields `title`, `note`, `due_at`, `recurrence`, `recur_from`, `retry_interval_min`, `max_retries`, and `#form-title`, `#submit-button`, `#cancel-edit`
  - `#search` (the `/` target), `#groups`, `#toasts`, `#theme-toggle`, `#shortcuts` (a `<dialog>`), `#refresh`
  - Per group: `<section class="group" data-group="overdue|today|upcoming|done">` containing `<h2>` and `<div class="cards">`
  - Per card: `.card.<status>` with `.title`, `.badge`, `.note`, `.meta`, `.actions`

**Rule that stays:** `textContent`, never `innerHTML`, for anything user-supplied. It is why this app has never needed HTML escaping.

- [x] **Step 1: Extract and extend the stylesheet**

Create `static/style.css` with the current `<style>` block's contents, then apply these changes:

```css
/* Theme is a data attribute so the toggle can override the OS preference.
   The media query only applies when no explicit choice has been stored. */
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #16181d; --muted: #6b7280;
  --line: #e3e6ea; --accent: #2f6fed; --pending: #b45309; --acked: #15803d;
  --expired: #9ca3af; --danger: #b91c1c; --overdue: #dc2626;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --panel: #1c1f26; --text: #e9ecf1; --muted: #9aa3b2;
    --line: #2a2f39; --accent: #5b8dff; --pending: #f0b429; --acked: #4ade80;
    --expired: #6b7280; --danger: #f87171; --overdue: #f87171;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --panel: #1c1f26; --text: #e9ecf1; --muted: #9aa3b2;
  --line: #2a2f39; --accent: #5b8dff; --pending: #f0b429; --acked: #4ade80;
  --expired: #6b7280; --danger: #f87171; --overdue: #f87171;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem 1rem 4rem; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 760px; margin: 0 auto; }
.page-head { display: flex; align-items: baseline; gap: .75rem; margin-bottom: 1.25rem; }
h1 { font-size: 1.4rem; margin: 0; flex: 1; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
     color: var(--muted); margin: 1.5rem 0 .6rem; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem; margin-bottom: 1.25rem;
}
label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: .25rem; }
input, textarea, select, button {
  font: inherit; color: inherit; background: var(--bg);
  border: 1px solid var(--line); border-radius: 7px; padding: .5rem .6rem; width: 100%;
}
textarea { resize: vertical; min-height: 3.5rem; }
.row { display: flex; gap: .75rem; flex-wrap: wrap; }
.row > * { flex: 1 1 10rem; }
.field { margin-bottom: .75rem; }
.hint { font-size: .75rem; color: var(--muted); margin: .25rem 0 0; }
button { cursor: pointer; }
button.primary {
  background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
}
button.primary:hover { filter: brightness(1.08); }
button.ghost { width: auto; background: transparent; padding: .35rem .7rem; font-size: .85rem; }
.icon-button { width: auto; background: transparent; border-color: transparent;
               padding: .3rem .5rem; font-size: 1rem; }
.icon-button:hover { border-color: var(--line); }
.toolbar { display: flex; gap: .5rem; align-items: center; margin-bottom: .5rem; }
.toolbar #search { flex: 1; }

.card {
  background: var(--panel); border: 1px solid var(--line); border-left-width: 4px;
  border-radius: 8px; padding: .75rem .9rem; margin-bottom: .6rem;
}
.card.pending { border-left-color: var(--pending); }
.card.acked { border-left-color: var(--acked); }
.card.expired { border-left-color: var(--expired); }
.card.is-overdue { border-left-color: var(--overdue); }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: .75rem; }
.title { font-weight: 600; }
.badge { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.badge.repeat { color: var(--accent); }
.note { color: var(--muted); margin: .3rem 0 0; white-space: pre-wrap; }
.meta { color: var(--muted); font-size: .8rem; margin-top: .45rem;
        display: flex; gap: .9rem; flex-wrap: wrap; align-items: center; }
.relative { color: var(--text); }
.actions { display: flex; gap: .4rem; margin-top: .6rem; flex-wrap: wrap; }
.actions button { width: auto; padding: .25rem .6rem; font-size: .8rem; }
.actions button.danger { color: var(--danger); border-color: transparent; background: transparent; }
.actions button.danger:hover { border-color: var(--danger); }
.empty { color: var(--muted); padding: .5rem 0; font-size: .9rem; }
.group[hidden] { display: none; }

#toasts {
  position: fixed; left: 50%; bottom: 1.25rem; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: .5rem; z-index: 10; width: min(28rem, 92vw);
}
.toast {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: .6rem .8rem; display: flex; gap: .75rem; align-items: center;
  box-shadow: 0 6px 20px rgb(0 0 0 / .18);
}
.toast span { flex: 1; }
.toast button { width: auto; padding: .2rem .6rem; font-size: .8rem; }
.toast.error { border-color: var(--danger); color: var(--danger); }

dialog { background: var(--panel); color: var(--text); border: 1px solid var(--line);
         border-radius: 10px; padding: 1.25rem; max-width: 26rem; }
dialog::backdrop { background: rgb(0 0 0 / .45); }
dialog kbd { background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
             padding: 0 .35rem; font: inherit; font-size: .85em; }
dialog ul { list-style: none; padding: 0; margin: .5rem 0 0; }
dialog li { display: flex; gap: .75rem; padding: .2rem 0; }
dialog li kbd { min-width: 2rem; text-align: center; }
```

- [x] **Step 2: Rewrite the markup**

Replace `static/index.html` in full:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reminders</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 100 100%27%3E%3Ctext y=%27.9em%27 font-size=%2790%27%3E%E2%8F%B0%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="/style.css">
</head>
<body>
<main>
  <div class="page-head">
    <h1>Reminders</h1>
    <button id="theme-toggle" class="icon-button" type="button"
            title="Toggle light/dark" aria-label="Toggle light/dark">◐</button>
    <button id="help-button" class="icon-button" type="button"
            title="Keyboard shortcuts" aria-label="Keyboard shortcuts">?</button>
  </div>

  <form class="panel" id="create-form">
    <div class="field">
      <label for="title">Title</label>
      <input id="title" name="title" required maxlength="200" placeholder="Take the bins out">
    </div>
    <div class="field">
      <label for="note">Note (optional)</label>
      <textarea id="note" name="note" maxlength="2000"></textarea>
    </div>
    <div class="field">
      <label for="due_at">Due</label>
      <input id="due_at" name="due_at" required maxlength="100"
             placeholder="tomorrow at 9am, in 2 hours, or 2026-08-20 18:00">
      <p class="hint" id="due-hint">Times are read in the server timezone.</p>
    </div>
    <div class="row">
      <div class="field">
        <label for="recurrence">Repeat (optional)</label>
        <select id="recurrence" name="recurrence">
          <option value="">Does not repeat</option>
          <option value="FREQ=DAILY">Every day</option>
          <option value="FREQ=DAILY;INTERVAL=3">Every 3 days</option>
          <option value="FREQ=WEEKLY">Every week</option>
          <option value="FREQ=WEEKLY;BYDAY=MO,WE,FR">Mon / Wed / Fri</option>
          <option value="FREQ=MONTHLY">Every month</option>
          <option value="FREQ=YEARLY">Every year</option>
        </select>
      </div>
      <div class="field">
        <label for="recur_from">Repeat counts from</label>
        <select id="recur_from" name="recur_from">
          <option value="schedule">The scheduled time</option>
          <option value="completion">When I complete it</option>
        </select>
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label for="retry_interval_min">Retry every (min)</label>
        <input id="retry_interval_min" name="retry_interval_min" type="number"
               min="1" max="1440" value="15">
      </div>
      <div class="field">
        <label for="max_retries">Max sends</label>
        <input id="max_retries" name="max_retries" type="number" min="1" max="100" value="4">
      </div>
    </div>
    <div class="actions">
      <button class="primary" id="submit-button" type="submit">Add reminder</button>
      <button class="ghost" id="cancel-edit" type="button" hidden>Cancel</button>
    </div>
  </form>

  <div class="toolbar">
    <input id="search" type="search" placeholder="Filter (press /)" aria-label="Filter reminders">
    <button id="refresh" class="ghost" type="button">Refresh</button>
  </div>

  <div id="groups">
    <section class="group" data-group="overdue" hidden><h2>Overdue</h2><div class="cards"></div></section>
    <section class="group" data-group="today" hidden><h2>Today</h2><div class="cards"></div></section>
    <section class="group" data-group="upcoming" hidden><h2>Upcoming</h2><div class="cards"></div></section>
    <section class="group" data-group="done" hidden><h2>Done &amp; expired</h2><div class="cards"></div></section>
  </div>
  <p class="empty" id="empty" hidden>Nothing here.</p>
</main>

<div id="toasts" aria-live="polite"></div>

<dialog id="shortcuts">
  <h2>Keyboard shortcuts</h2>
  <ul>
    <li><kbd>n</kbd> <span>New reminder</span></li>
    <li><kbd>/</kbd> <span>Filter the list</span></li>
    <li><kbd>Esc</kbd> <span>Close / cancel editing</span></li>
    <li><kbd>?</kbd> <span>This help</span></li>
  </ul>
  <div class="actions"><button class="ghost" id="shortcuts-close" type="button">Close</button></div>
</dialog>

<script src="/app.js"></script>
</body>
</html>
```

**Note:** `due_at` is now a free-text input rather than `datetime-local`, because the server accepts natural language and a native picker cannot express "in 2 hours". Task 16 pre-fills it with a concrete resolved time so the default stays unambiguous.

- [x] **Step 3: Verify the shell loads**

Run:

```bash
.venv/bin/python -m pytest tests/test_main.py -q
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799 &
sleep 3
curl -s -o /dev/null -w 'index %{http_code}\n' http://127.0.0.1:8799/
curl -s -o /dev/null -w 'css   %{http_code}\n' http://127.0.0.1:8799/style.css
kill %1
```

Expected: `index 200`, `css 200`, and the existing static-file test still passing. The page will render unstyled-but-structured content and an empty list — `app.js` is still the old version and will log errors about missing elements. That is expected until Task 16.

- [x] **Step 4: Commit**

```bash
git add static/style.css static/index.html
git commit -m "feat(dashboard): grouped-view markup, extracted stylesheet, theme hook"
```

---

### Task 16: Dashboard behaviour — actions, grouping, toasts, shortcuts

**Files:**
- Modify: `static/app.js` (full rewrite)

**Interfaces:**
- Consumes: the DOM contract from Task 15, and `GET /api/config`, `GET|POST /api/reminders`, `GET|PATCH|DELETE /api/reminders/{id}`, `POST /api/reminders/{id}/complete`, `POST /api/reminders/{id}/snooze`.
- Produces: no exports — this is a leaf.

**Four behaviours worth stating before the code:**

1. **Undo-delete is a *deferred* delete, not a re-create.** The card vanishes at once and the `DELETE` fires ~6s later unless Undo cancels it, so an undone delete keeps the reminder's original id and history. A `pagehide` handler flushes anything still pending with `keepalive: true` so closing the tab does not silently cancel a delete.
2. **Polling must not resurrect a pending delete**, hence `pendingDeletes`.
3. **Grouping day boundaries use the server timezone**, compared as `en-CA` date strings (`YYYY-MM-DD`) — exact, and it sidesteps every off-by-one that arithmetic on local `Date` objects invites.
4. **`textContent` only.** No `innerHTML` anywhere a title, note, or recurrence rule can reach.

- [x] **Step 1: Rewrite the script**

Replace `static/app.js` in full:

```javascript
const API = "/api/reminders";
const POLL_MS = 10000;
const UNDO_MS = 6000;

let config = { timezone: "UTC", default_snooze_min: 15, max_snoozes: 20 };
let reminders = [];
let editingId = null;
let filterText = "";
const pendingDeletes = new Map();   // id -> timeout handle

// --- formatting ----------------------------------------------------------

/** YYYY-MM-DD for an instant, in the server's timezone. */
function dayKey(date) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: config.timezone }).format(date);
}

/** Absolute time in the server's timezone. */
function formatAbsolute(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    timeZone: config.timezone,
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** "in 2h" / "3 days ago" — the reading that actually answers "is this urgent?". */
function formatRelative(iso, now = new Date()) {
  if (!iso) return "";
  const seconds = Math.round((new Date(iso) - now) / 1000);
  const units = [
    ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1],
  ];
  const relative = new Intl.RelativeTimeFormat([], { numeric: "auto", style: "narrow" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return relative.format(Math.round(seconds / size), unit);
    }
  }
  return "";
}

const FREQ_NOUNS = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };
const DAY_NAMES = {
  MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun",
};

/** "FREQ=WEEKLY;BYDAY=MO,WE" -> "every week on Mon, Wed". */
function describeRecurrence(rule) {
  if (!rule) return "";
  const parts = Object.fromEntries(
    rule.split(";").filter(Boolean).map((chunk) => {
      const [key, value] = chunk.split("=");
      return [key.toUpperCase(), (value || "").toUpperCase()];
    })
  );
  const noun = FREQ_NOUNS[parts.FREQ];
  if (!noun) return rule;
  const interval = Number(parts.INTERVAL || 1);
  let text = interval === 1 ? `every ${noun}` : `every ${interval} ${noun}s`;
  if (parts.BYDAY) {
    const days = parts.BYDAY.split(",").map((code) => DAY_NAMES[code] || code);
    text += ` on ${days.join(", ")}`;
  }
  return text;
}

// --- toasts --------------------------------------------------------------

function toast(message, { error = false, actionLabel = null, onAction = null } = {}) {
  const element = document.createElement("div");
  element.className = error ? "toast error" : "toast";

  const text = document.createElement("span");
  text.textContent = message;                       // textContent, never innerHTML
  element.append(text);

  const dismiss = () => element.remove();

  if (actionLabel) {
    const action = document.createElement("button");
    action.textContent = actionLabel;
    action.addEventListener("click", () => { onAction(); dismiss(); });
    element.append(action);
  }

  document.getElementById("toasts").append(element);
  setTimeout(dismiss, error ? 6000 : UNDO_MS);
  return element;
}

/** Turn a failed response into the server's own message where there is one. */
async function reportFailure(response, fallback) {
  const body = await response.json().catch(() => null);
  const detail = body && body.detail;
  toast(typeof detail === "string" ? detail : fallback, { error: true });
}

// --- data ----------------------------------------------------------------

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) return;
  config = await response.json();
  const hint = document.getElementById("due-hint");
  hint.textContent =
    `Times are read in ${config.timezone}. Try "tomorrow at 9am" or "in 2 hours".`;
}

async function loadReminders() {
  const response = await fetch(API);
  if (!response.ok) {
    toast("Could not load reminders.", { error: true });
    return;
  }
  reminders = await response.json();
  render();
}

// --- rendering -----------------------------------------------------------

function groupOf(reminder, now) {
  if (reminder.status !== "pending") return "done";
  const due = new Date(reminder.due_at);
  if (due < now) return "overdue";
  return dayKey(due) === dayKey(now) ? "today" : "upcoming";
}

function matchesFilter(reminder) {
  if (!filterText) return true;
  const haystack = `${reminder.title} ${reminder.note || ""}`.toLowerCase();
  return haystack.includes(filterText);
}

function render() {
  const now = new Date();
  const buckets = { overdue: [], today: [], upcoming: [], done: [] };
  for (const reminder of reminders) {
    if (pendingDeletes.has(reminder.id)) continue;
    if (!matchesFilter(reminder)) continue;
    buckets[groupOf(reminder, now)].push(reminder);
  }

  let total = 0;
  for (const section of document.querySelectorAll(".group")) {
    const bucket = buckets[section.dataset.group];
    const cards = section.querySelector(".cards");
    cards.replaceChildren(...bucket.map((r) => buildCard(r, now)));
    section.hidden = bucket.length === 0;
    total += bucket.length;
  }
  document.getElementById("empty").hidden = total > 0;
}

function span(text, className) {
  const element = document.createElement("span");
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function actionButton(label, className, handler) {
  const button = document.createElement("button");
  button.textContent = label;
  if (className) button.className = className;
  button.addEventListener("click", handler);
  return button;
}

function buildCard(reminder, now) {
  const card = document.createElement("div");
  card.className = `card ${reminder.status}`;
  if (reminder.status === "pending" && new Date(reminder.due_at) < now) {
    card.classList.add("is-overdue");
  }

  const head = document.createElement("div");
  head.className = "card-head";
  head.append(span(reminder.title, "title"));
  if (reminder.recurrence) {
    head.append(span(describeRecurrence(reminder.recurrence), "badge repeat"));
  }
  head.append(span(reminder.status, "badge"));
  card.append(head);

  if (reminder.note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = reminder.note;
    card.append(note);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.append(
    span(formatRelative(reminder.due_at, now), "relative"),
    span(formatAbsolute(reminder.due_at)),
    span(`sent ${reminder.retry_count}/${reminder.max_retries}`),
    span(`every ${reminder.retry_interval_min}m`),
  );
  if (reminder.snooze_count > 0) {
    meta.append(span(`snoozed ${reminder.snooze_count}×`));
  }
  card.append(meta);

  const actions = document.createElement("div");
  actions.className = "actions";
  if (reminder.status === "pending") {
    actions.append(
      actionButton("✅ Done", null, () => completeReminder(reminder)),
      actionButton(`💤 ${config.default_snooze_min}m`, null, () => snoozeReminder(reminder)),
      actionButton("Edit", "ghost", () => startEdit(reminder)),
    );
  }
  actions.append(actionButton("Delete", "danger", () => deleteReminder(reminder)));
  card.append(actions);

  return card;
}

// --- actions -------------------------------------------------------------

async function completeReminder(reminder) {
  const response = await fetch(`${API}/${reminder.id}/complete`, { method: "POST" });
  if (!response.ok) return reportFailure(response, "Could not complete that reminder.");
  const updated = await response.json();
  toast(
    updated.status === "pending"
      ? `Done — next on ${formatAbsolute(updated.due_at)}.`
      : `Done: ${updated.title}`
  );
  loadReminders();
}

async function snoozeReminder(reminder) {
  const response = await fetch(`${API}/${reminder.id}/snooze`, { method: "POST" });
  if (!response.ok) return reportFailure(response, "Could not snooze that reminder.");
  const updated = await response.json();
  toast(`Snoozed until ${formatAbsolute(updated.due_at)}.`);
  loadReminders();
}

/** Deferred delete: the card goes now, the request goes in UNDO_MS. */
function deleteReminder(reminder) {
  const handle = setTimeout(() => commitDelete(reminder.id), UNDO_MS);
  pendingDeletes.set(reminder.id, handle);
  render();

  toast(`Deleted “${reminder.title}”.`, {
    actionLabel: "Undo",
    onAction: () => {
      clearTimeout(pendingDeletes.get(reminder.id));
      pendingDeletes.delete(reminder.id);
      render();
    },
  });
}

async function commitDelete(id, keepalive = false) {
  pendingDeletes.delete(id);
  const response = await fetch(`${API}/${id}`, { method: "DELETE", keepalive });
  if (!response.ok && !keepalive) {
    toast("Delete failed.", { error: true });
  }
  if (!keepalive) loadReminders();
}

// Closing the tab must not silently cancel a delete the user already confirmed.
window.addEventListener("pagehide", () => {
  for (const [id, handle] of pendingDeletes) {
    clearTimeout(handle);
    commitDelete(id, true);
  }
});

// --- the form ------------------------------------------------------------

function form() {
  return document.getElementById("create-form");
}

/** Add an option for a rule the preset list does not contain (e.g. one
 *  created over MCP), so editing never silently drops it. */
function ensureRecurrenceOption(rule) {
  const select = document.getElementById("recurrence");
  if (!rule || [...select.options].some((option) => option.value === rule)) return;
  const option = document.createElement("option");
  option.value = rule;
  option.textContent = describeRecurrence(rule);
  select.append(option);
}

function startEdit(reminder) {
  editingId = reminder.id;
  const f = form();
  f.title.value = reminder.title;
  f.note.value = reminder.note || "";
  f.due_at.value = new Date(reminder.due_at).toISOString();
  ensureRecurrenceOption(reminder.recurrence);
  f.recurrence.value = reminder.recurrence || "";
  f.recur_from.value = reminder.recur_from;
  f.retry_interval_min.value = reminder.retry_interval_min;
  f.max_retries.value = reminder.max_retries;

  document.getElementById("submit-button").textContent = "Save changes";
  document.getElementById("cancel-edit").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "start" });
  f.title.focus();
}

function cancelEdit() {
  editingId = null;
  form().reset();
  resetDefaults();
  document.getElementById("submit-button").textContent = "Add reminder";
  document.getElementById("cancel-edit").hidden = true;
}

async function submitForm(event) {
  event.preventDefault();
  const f = event.target;
  const payload = {
    title: f.title.value.trim(),
    note: f.note.value.trim() || null,
    due_at: f.due_at.value.trim(),
    recurrence: f.recurrence.value || null,
    recur_from: f.recur_from.value,
    retry_interval_min: Number(f.retry_interval_min.value),
    max_retries: Number(f.max_retries.value),
  };
  if (!payload.due_at) {
    toast("Give it a due time.", { error: true });
    return;
  }

  const editing = editingId !== null;
  const response = await fetch(editing ? `${API}/${editingId}` : API, {
    method: editing ? "PATCH" : "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    return reportFailure(response, "Could not save that reminder.");
  }

  const saved = await response.json();
  // Echo the resolved time: a natural-language misparse has to be visible now,
  // not days later as a reminder that never arrived.
  toast(`${editing ? "Updated" : "Added"} “${saved.title}” for ${formatAbsolute(saved.due_at)}.`);
  cancelEdit();
  loadReminders();
}

function resetDefaults() {
  const inFifteen = new Date(Date.now() + 15 * 60 * 1000);
  inFifteen.setSeconds(0, 0);
  document.getElementById("due_at").value = inFifteen.toISOString();
  document.getElementById("recurrence").value = "";
  document.getElementById("recur_from").value = "schedule";
  document.getElementById("retry_interval_min").value = 15;
  document.getElementById("max_retries").value = 4;
}

// --- theme ---------------------------------------------------------------

function applyTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function toggleTheme() {
  // Three states, cycled: OS preference -> light -> dark -> OS preference.
  const current = localStorage.getItem("theme");
  const next = current === null ? "light" : current === "light" ? "dark" : null;
  if (next === null) {
    localStorage.removeItem("theme");
  } else {
    localStorage.setItem("theme", next);
  }
  applyTheme(next);
}

// --- wiring --------------------------------------------------------------

function isTyping(target) {
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function wireShortcuts() {
  const dialog = document.getElementById("shortcuts");
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (dialog.open) dialog.close();
      else if (editingId !== null) cancelEdit();
      else if (document.activeElement === document.getElementById("search")) {
        document.getElementById("search").blur();
      }
      return;
    }
    if (isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key === "n") {
      event.preventDefault();
      cancelEdit();
      document.getElementById("title").focus();
    } else if (event.key === "/") {
      event.preventDefault();
      document.getElementById("search").focus();
    } else if (event.key === "?") {
      event.preventDefault();
      dialog.showModal();
    }
  });
  document.getElementById("help-button").addEventListener("click", () => dialog.showModal());
  document.getElementById("shortcuts-close").addEventListener("click", () => dialog.close());
}

function wire() {
  form().addEventListener("submit", submitForm);
  document.getElementById("cancel-edit").addEventListener("click", cancelEdit);
  document.getElementById("refresh").addEventListener("click", loadReminders);
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("search").addEventListener("input", (event) => {
    filterText = event.target.value.trim().toLowerCase();
    render();
  });
  wireShortcuts();
}

async function start() {
  applyTheme(localStorage.getItem("theme"));
  wire();
  resetDefaults();
  await loadConfig();
  await loadReminders();
  setInterval(loadReminders, POLL_MS);
}

start();
```

- [x] **Step 2: Syntax-check the script**

Run: `node --check static/app.js`
Expected: no output (exit 0). A parse error here is the one class of dashboard bug that no backend test would catch.

- [x] **Step 3: Verify in a browser**

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799
```

Open `http://127.0.0.1:8799/` and confirm each of these, which map one-to-one onto spec §12:

- [x] Adding "test one" due "in 2 hours" succeeds, and the toast names the **resolved absolute time**.
- [x] An unparseable due time ("sometime soonish") shows the server's error in a red toast and does not create anything.
- [x] Adding one due "in 1 minute" with repeat "Every day" shows an `every day` badge on the card.
- [x] The list is split into **Overdue / Today / Upcoming**, and empty groups are hidden.
- [x] Each card shows a relative time ("in 2h") beside the absolute one.
- [x] **Done** on the recurring reminder keeps it pending and moves `due_at` forward one day.
- [x] **Done** on a one-shot reminder moves it to **Done & expired**.
- [x] **Snooze** pushes the time out by the configured default and the card shows `snoozed 1×`.
- [x] **Edit** loads the reminder into the form, the button reads "Save changes", and Cancel restores "Add reminder".
- [x] **Delete** removes the card immediately and shows an **Undo** toast; Undo restores the same card with the same id; leaving the toast alone makes the delete stick after ~6s.
- [x] `n` focuses the title, `/` focuses the filter, `?` opens the shortcuts dialog, `Esc` closes it.
- [x] Typing in the filter narrows the cards by title and note.
- [x] The ◐ button cycles OS preference → light → dark and the choice survives a reload.

- [x] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): complete/snooze/edit, grouped views, toasts, undo, shortcuts"
```

---

### Task 17: Documentation and deployment

**Files:**
- Modify: `README.md`
- Modify: `.env.example` (verify Task 1's additions are complete)
- Verify: `requirements.txt`

**Interfaces:**
- Consumes: everything.
- Produces: a running CT 108 with the connector reachable, and a `~/.claude/projects/-home-redji/memory/reminder-service.md` that lets the next session start cold.

- [x] **Step 1: Confirm the full suite and record the real count**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS. Write the actual number into the README (spec §16 asks for the real count, not the ~130 estimate).

- [x] **Step 2: Update the README**

Add a **Configuration** section documenting every setting from Global Constraints, with its default and its effect, and note that all are optional and default to the pre-existing behaviour.

Add a **Recurring reminders** section:

```markdown
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
```

Add a **Claude connector** section:

```markdown
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
```

- [x] **Step 3: Rebuild locally and smoke-test the container**

```bash
cd ~/reminder-service
docker compose build && docker compose up -d --force-recreate
sleep 8
docker compose logs --tail 30
curl -s http://127.0.0.1:8765/api/healthz
curl -s http://127.0.0.1:8765/api/config
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Expected: `{"status":"ok"}`, a config JSON with `"timezone":"UTC"`, and `200` from `/mcp`. The logs must show the migration line and **must not** contain `BOT_TOKEN`.

- [x] **Step 4: Commit and push**

```bash
git add README.md .env.example requirements.txt
git commit -m "docs: recurrence, connector, and configuration reference"
git push origin main
```

- [x] **Step 5: Back up production before deploying**

The migration is additive and idempotent, but it runs against live data on first boot. Take a copy first — it costs one command and it is the only thing standing between a bad migration and lost reminders.

```bash
ssh root@192.168.1.206 'cd /opt/reminder-service && cp data/reminders.db data/reminders.db.pre-v1.bak && ls -la data/'
```

- [x] **Step 6: Deploy to CT 108**

```bash
ssh root@192.168.1.206 'cd /opt/reminder-service && git pull && docker compose build && docker compose up -d --force-recreate'
sleep 10
ssh root@192.168.1.206 'cd /opt/reminder-service && docker compose logs --tail 40'
```

Expected in the logs: the migration line (`migrated schema from user_version 0 to 1`), `MCP connector mounted at /mcp`, `telegram bot polling`, and `scheduler started`. If startup aborted, the migration failed — restore `data/reminders.db.pre-v1.bak`, redeploy the previous image, and debug from the copy rather than from prod.

- [x] **Step 7: Verify the deployment (read-only calls only)**

```bash
curl -s http://192.168.1.206:8765/api/healthz
curl -s http://192.168.1.206:8765/api/config
curl -s http://192.168.1.206:8765/api/reminders | head -c 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://192.168.1.206:8765/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
ssh root@192.168.1.206 'docker exec reminder-service python -c "import sqlite3; c=sqlite3.connect(\"/data/reminders.db\"); print(\"user_version\", c.execute(\"PRAGMA user_version\").fetchone()[0])"'
```

Expected: healthz ok, config JSON, the pre-existing reminders listed **with their original ids, titles, and statuses**, `200` from `/mcp`, and `user_version 1`.

**Read-only only.** A probe `PUT` on a live service has clobbered real credentials on this project before — do not issue mutating calls against prod to "check it works".

- [ ] **Step 8: Hand the public check to the user** (open — needs the user's phone on cell data)

The public Funnel path **cannot be verified from this machine** — the intranet blocks `*.ts.net`, so every local request loops back over the tailnet and proves nothing about the public route. Ask the user to, from a phone on **cell data** (Wi-Fi off):

1. Open `https://reminder.tail78f4cc.ts.net/` and confirm the dashboard loads.
2. Add the connector on claude.ai at `https://reminder.tail78f4cc.ts.net/mcp` and ask Claude "what's due today?".

If the connector fails to add, the two known causes are a 421 (transport-security setting missing from Task 13) and a 307 (a `Mount` slipped in where a `Route` belongs).

- [x] **Step 9: Update the durable notes**

Update `~/.claude/projects/-home-redji/memory/reminder-service.md`:

- Replace the "Sub-project: MCP connector..." section's `NEXT ACTION` with the completed state and the connector URL.
- Keep the verified-facts list; add the MCP mount landmines from Task 13 and the `next monday` dateparser quirk if they are not already there.
- Record the new settings and their defaults.
- Note the prod backup path `data/reminders.db.pre-v1.bak` and that `user_version` is now `1`.

- [x] **Step 10: Final commit**

```bash
git add -A && git status
git commit -m "chore: post-deploy notes" || true
git push origin main
```

---

## Self-review

Run through this before handing the plan to an executor; it is a checklist, not a subagent dispatch.

**Spec coverage — every section maps to a task:**

| Spec section | Task(s) |
|---|---|
| §5.1 MCP transport, `MCP_ENABLED` | 1, 13 |
| §5.2 Service layer extraction | 7, 8, 9, 11 |
| §6.1 New reminder columns | 2 |
| §6.2 `completions` table | 2 |
| §6.3 Migration, tested against real prod schema | 3 |
| §7.1 RRULE subset + rejection by name | 6 |
| §7.2 Both anchors; BYDAY×completion rejected | 6 |
| §7.3 Roll-forward on completion **and** expiry | 8, 9, 10 |
| §8.1 `TIMEZONE`, fail-fast, `/api/config` | 1, 11 |
| §8.2 Quiet hours incl. midnight crossing | 1, 5, 10 |
| §8.3 Snooze: default, cap, three entry points | 9, 11, 14, 16 |
| §9 NL dates; echoed resolved times; errors not guesses | 4, 11, 12, 16 |
| §10 Nine MCP tools; timezone in descriptions; actionable errors | 12 |
| §11 REST additions, no `next_due_at` | 11 |
| §12 Dashboard overhaul | 15, 16 |
| §13 Telegram snooze + recurrence in body | 14 |
| §14 Configuration table | 1, 17 |
| §15 Error handling at each edge | 4, 10, 11, 12, 14 |
| §16 Testing priorities | every task (TDD) |
| §17 Deployment | 17 |

**Type and name consistency, checked across tasks:**

- `Settings.timezone` is a `str` everywhere; `Settings.tzinfo` is the `ZoneInfo`. Tasks 10, 11, 12, 14 all pass `settings.timezone` (the string) as `tz=`.
- `next_occurrence(*, rule, recur_from, previous_due, resolved_at, now, tz)` — defined in Task 6, called only from `_resolve_occurrence` in Task 8.
- `_resolve_occurrence(session, reminder, *, outcome, resolved_at, tz, terminal_status)` — defined in Task 8, used by `complete_reminder` (Task 8) and `expire_reminder` (Task 9).
- `expire_reminder` and `record_send` both **do not commit**; `create/update/delete/complete/snooze` all **do**. The scheduler (Task 10) relies on exactly that split.
- `service.list_reminders(status=...)` takes a **string**, not a `ReminderStatus`; the router converts with `.value` (Task 11) and the MCP tool passes the string straight through (Task 12).
- `as_local_iso(dt, tz)` (Task 4) is used by the router's `/api/config`, the MCP layer, and the bot. `as_utc_iso(dt)` keeps its one-argument shape and stays the REST read format.
- Every error `service.py` raises is a `ServiceError` subclass — including `InvalidField`, which is deliberately **not** a bare `ValueError`, so it reaches the one error-mapping table rather than surfacing as a 500.
- `SNOOZE_PREFIX` (Task 14) is distinct from the existing `CALLBACK_PREFIX`; the bot's callback dispatch checks both.
- The DOM ids in Task 15 and the selectors in Task 16 match one-for-one: `#create-form`, `#submit-button`, `#cancel-edit`, `#search`, `#groups`, `.group[data-group]`, `.cards`, `#empty`, `#toasts`, `#theme-toggle`, `#help-button`, `#shortcuts`, `#shortcuts-close`, `#refresh`, `#due-hint`.

**Deliberate divergences from the spec, each argued in "Plan-level decisions" above:** errors live in `app/errors.py`; tool *descriptions* carry the timezone while tool *results* carry the current time; the dashboard's `/` shortcut focuses a minimal client-side filter rather than a deferred search UI.

**Risks the plan does not close** (spec §18, restated so the executor does not think they were missed):

- The public Funnel route is unverifiable from this network — Task 17 Step 8 hands it to the user.
- Recurrence roll-forward is the one place a bug either spams or silently stops a series; both directions are covered by explicit tests in Tasks 8, 9, and 10, and the expiry path is tested precisely because that is the failure most likely to erode trust.
