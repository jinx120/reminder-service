# Reminder Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted single-container reminder dashboard that nags you over Telegram on a retry schedule until you acknowledge, then stops.

**Architecture:** One FastAPI process hosts everything: a REST API over SQLite (SQLModel), a static vanilla-JS dashboard, an APScheduler job that ticks every 30s to decide which reminders need sending or expiring, and a python-telegram-bot long-polling Application. All three run in a single asyncio event loop wired up in the FastAPI `lifespan` handler, so the scheduler can `await` Telegram sends directly. The retry/expiry decision is a **pure function** (`app/logic.py`) with no DB or network, which is where nearly all the test coverage lives; the scheduler is a thin shell that applies the decision. Telegram sending is injected into the scheduler as a `Sender` callable so tests never touch the network.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, SQLModel/SQLAlchemy, SQLite, APScheduler 3.x (`AsyncIOScheduler`), python-telegram-bot 21+, vanilla JS, Docker Compose.

## Global Constraints

- **Project root:** `/home/redji/reminder-service`. All paths in this plan are relative to it.
- **Deploy scope for this plan:** build and verify on this host (`clawd`) via `docker compose`. Provisioning a Proxmox LXC is explicitly a **follow-up session**, not part of this plan.
- **Host port:** `8765` (host) -> `8000` (container). Port 8000 on this host is already taken by `swingbot`; do not use it.
- **Time storage:** every datetime stored in SQLite is **naive UTC**. Conversion happens only at the boundaries (`app/timeutil.py`). Never store a tz-aware datetime; never compare a naive to an aware datetime.
- **Enum columns:** `status` is stored as a **plain `str` column** holding the enum's *value* (`"pending"`/`"acked"`/`"expired"`). Do NOT use SQLAlchemy's `Enum` type — it validates by member *name*, not value, and that mismatch has caused a production outage on this host before.
- **Single worker:** uvicorn must run with `--workers 1`. Two workers means two schedulers and two polling loops, i.e. duplicate Telegram messages and a `Conflict` error from the Bot API.
- **Telegram is optional at runtime:** if `BOT_TOKEN` or `CHAT_ID` is unset, the app must still boot; it logs notifications instead of sending them. This is what makes Tasks 1-11 testable before the bot exists.
- **Messages are plain text** (no Markdown/HTML parse mode) so user-supplied titles never break message formatting.
- **Out of scope for v1** (mention if raised, do not build): multi-user/multi-chat, recurring reminders, SMS fallback, dashboard auth beyond network-level access.

### Spec deviations (deliberate, with reasons)

1. **`AsyncIOScheduler`, not `BackgroundScheduler`.** The spec names `BackgroundScheduler`, but that is thread-based, and python-telegram-bot v20+ is async. A thread-based scheduler cannot `await bot.send_message` without cross-thread event-loop juggling. `AsyncIOScheduler` runs the job as a coroutine on the same loop as FastAPI and the bot. Everything stays single-threaded and simple.
2. **Expiry requires one more elapsed interval.** The spec's pseudocode marks a reminder `expired` as soon as `retry_count >= max_retries`, which on the very next tick (up to 30s after the final message) would expire it — leaving no realistic window to tap Done on the last nag. Corrected rule: expire only once `retry_count >= max_retries` **and** a full `retry_interval_min` has elapsed since `last_sent_at`. You get one full interval after the last message.
3. **`max_retries` counts total sends.** Following the spec's pseudocode literally, the first send also increments `retry_count`, so `max_retries=4` yields 4 total messages, not 1 + 4. Documented as-is; the field is described in the UI as "max sends".
4. **`notifications` gains a `telegram_message_id` column.** The spec wants the original message edited to show "Done at HH:MM". For the inline-button ack the callback carries its own message, but for the plain-text-reply ack path there is no such handle — so the message id has to be persisted at send time.
5. **A bare text reply acks the most recently *notified* pending reminder.** The spec says "any plain-text reply while a reminder is still pending" without saying which one. Pinned rule: the pending reminder with the newest non-null `last_sent_at` (ties broken by highest id). If no pending reminder has ever been sent, the bot replies "Nothing pending." and acks nothing.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/config.py` | `Settings` dataclass loaded from env; `bot_enabled` flag |
| `app/timeutil.py` | UTC naive/aware conversion — the only place tz logic lives |
| `app/models.py` | SQLModel tables: `Reminder`, `Notification`, `ReminderStatus` |
| `app/schemas.py` | Pydantic request/response models + `to_read()` serialisers (kept apart from `models.py` because `sqlmodel.Field` and `pydantic.Field` collide) |
| `app/db.py` | `Database` class: engine, `create_all()`, `session()` contextmanager |
| `app/logic.py` | **Pure** `decide(...) -> Action`. No DB, no clock, no network |
| `app/service.py` | DB operations shared by bot and scheduler: `ack_reminder`, `find_reply_ack_target` |
| `app/scheduler.py` | `tick()` (applies `decide` + `Sender`), `build_scheduler()`, `log_sender` |
| `app/bot.py` | PTB `Application`, handlers, `send_reminder_message()` |
| `app/routers/reminders.py` | REST CRUD |
| `app/main.py` | FastAPI app, `lifespan` wiring of db + bot + scheduler, static mount |
| `static/index.html`, `static/app.js` | Dashboard |
| `tests/` | pytest suite; `conftest.py` provides in-memory DB + TestClient fixtures |

Import direction is strictly one-way, so there are no cycles:
`config`/`timeutil` -> `models`/`schemas` -> `db` -> `logic` -> `service` -> `scheduler`/`bot`/`routers` -> `main`.
`bot.py` and `scheduler.py` never import each other; `main.py` connects them by passing `bot.send_reminder_message` into the scheduler as the `Sender`.

---

### Task 1: Project scaffold, dependencies, and settings

**Files:**
- Create: `requirements.txt`, `.env.example`, `.gitignore`, `app/__init__.py`, `app/routers/__init__.py`, `app/config.py`, `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `app.config.Settings` (frozen dataclass with fields `bot_token: str | None`, `chat_id: int | None`, `db_path: str`, `tick_seconds: int`, `default_retry_interval_min: int`, `default_max_retries: int`, and property `bot_enabled: bool`) and `app.config.load_settings() -> Settings`

- [ ] **Step 1: Create the directory skeleton and git ignore rules**

```bash
cd /home/redji/reminder-service
mkdir -p app/routers static data tests
touch app/__init__.py app/routers/__init__.py tests/__init__.py
cat > .gitignore <<'EOF'
__pycache__/
*.py[cod]
.venv/
.env
data/*.db
data/*.db-journal
.pytest_cache/
EOF
```

- [ ] **Step 2: Write `requirements.txt`**

```
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
sqlmodel>=0.0.22,<0.1
apscheduler>=3.10,<4.0
python-telegram-bot>=21.0,<23.0
python-dotenv>=1.0,<2.0
pytest>=8.0,<9.0
pytest-asyncio>=0.24,<1.0
httpx>=0.27,<1.0
```

APScheduler is pinned below 4.0 deliberately: the 4.x line renamed and restructured the scheduler API, and `AsyncIOScheduler` as used here is the 3.x interface.

- [ ] **Step 3: Create the venv and install**

```bash
cd /home/redji/reminder-service
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Expected: installs cleanly. Verify with `.venv/bin/python -c "import fastapi, sqlmodel, apscheduler, telegram; print('deps ok')"` -> prints `deps ok`.

- [ ] **Step 4: Write `.env.example`**

```bash
cat > .env.example <<'EOF'
# Get BOT_TOKEN from @BotFather on Telegram (/newbot)
BOT_TOKEN=
# Your numeric Telegram chat id (see README for how to find it)
CHAT_ID=
# Optional tuning
DB_PATH=data/reminders.db
TICK_SECONDS=30
DEFAULT_RETRY_INTERVAL_MIN=15
DEFAULT_MAX_RETRIES=4
EOF
```

- [ ] **Step 5: Add pytest config to `pyproject.toml`**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`asyncio_mode = "auto"` means async test functions run without needing an `@pytest.mark.asyncio` decorator on each one. Every async test in this plan relies on it.

- [ ] **Step 6: Write the failing test** — `tests/test_config.py`

```python
from app.config import load_settings


def test_defaults_when_env_is_empty(monkeypatch):
    for key in ("BOT_TOKEN", "CHAT_ID", "DB_PATH", "TICK_SECONDS",
                "DEFAULT_RETRY_INTERVAL_MIN", "DEFAULT_MAX_RETRIES"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings()
    assert s.bot_token is None
    assert s.chat_id is None
    assert s.db_path == "data/reminders.db"
    assert s.tick_seconds == 30
    assert s.default_retry_interval_min == 15
    assert s.default_max_retries == 4
    assert s.bot_enabled is False


def test_env_overrides_and_chat_id_is_int(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CHAT_ID", "987654321")
    monkeypatch.setenv("DB_PATH", "/data/x.db")
    monkeypatch.setenv("TICK_SECONDS", "60")
    s = load_settings()
    assert s.bot_token == "123:abc"
    assert s.chat_id == 987654321
    assert s.db_path == "/data/x.db"
    assert s.tick_seconds == 60
    assert s.bot_enabled is True


def test_bot_disabled_when_only_token_present(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.delenv("CHAT_ID", raising=False)
    assert load_settings().bot_enabled is False


def test_empty_string_env_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("CHAT_ID", "")
    s = load_settings()
    assert s.bot_token is None
    assert s.chat_id is None
    assert s.bot_enabled is False
```

The last test matters: `.env.example` ships `BOT_TOKEN=` with an empty value, so "set but empty" is the *normal* pre-setup state and must not crash `int("")`.

- [ ] **Step 7: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 8: Write `app/config.py`**

```python
import os
from dataclasses import dataclass


def _env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace-only as unset."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    chat_id: int | None
    db_path: str
    tick_seconds: int
    default_retry_interval_min: int
    default_max_retries: int

    @property
    def bot_enabled(self) -> bool:
        return self.bot_token is not None and self.chat_id is not None


def load_settings() -> Settings:
    chat_id = _env("CHAT_ID")
    return Settings(
        bot_token=_env("BOT_TOKEN"),
        chat_id=int(chat_id) if chat_id is not None else None,
        db_path=_env("DB_PATH") or "data/reminders.db",
        tick_seconds=_env_int("TICK_SECONDS", 30),
        default_retry_interval_min=_env_int("DEFAULT_RETRY_INTERVAL_MIN", 15),
        default_max_retries=_env_int("DEFAULT_MAX_RETRIES", 4),
    )
```

- [ ] **Step 9: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS, 4 passed

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: project scaffold, dependencies, and env-driven settings"
```

---

### Task 2: UTC time utilities

**Files:**
- Create: `app/timeutil.py`
- Test: `tests/test_timeutil.py`

**Interfaces:**
- Consumes: nothing
- Produces: `utcnow() -> datetime` (naive UTC), `to_utc_naive(dt: datetime) -> datetime`, `as_utc_iso(dt: datetime | None) -> str | None`

- [ ] **Step 1: Write the failing test** — `tests/test_timeutil.py`

```python
from datetime import datetime, timedelta, timezone

from app.timeutil import as_utc_iso, to_utc_naive, utcnow


def test_utcnow_is_naive_and_close_to_now():
    now = utcnow()
    assert now.tzinfo is None
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(reference - now) < timedelta(seconds=5)


def test_to_utc_naive_converts_aware_datetime():
    aware = datetime(2026, 8, 12, 15, 30, tzinfo=timezone(timedelta(hours=5)))
    assert to_utc_naive(aware) == datetime(2026, 8, 12, 10, 30)
    assert to_utc_naive(aware).tzinfo is None


def test_to_utc_naive_passes_naive_through_unchanged():
    naive = datetime(2026, 8, 12, 15, 30)
    assert to_utc_naive(naive) == naive


def test_to_utc_naive_handles_negative_offset():
    aware = datetime(2026, 8, 12, 1, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert to_utc_naive(aware) == datetime(2026, 8, 12, 7, 0)


def test_as_utc_iso_marks_the_value_as_utc():
    assert as_utc_iso(datetime(2026, 8, 12, 10, 30)) == "2026-08-12T10:30:00+00:00"


def test_as_utc_iso_passes_none_through():
    assert as_utc_iso(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_timeutil.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.timeutil'`

- [ ] **Step 3: Write `app/timeutil.py`**

```python
from datetime import datetime, timezone


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_timeutil.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/timeutil.py tests/test_timeutil.py
git commit -m "feat: UTC-naive time helpers"
```

---

### Task 3: Data model, schemas, and database

**Files:**
- Create: `app/models.py`, `app/schemas.py`, `app/db.py`
- Test: `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `app.timeutil.utcnow`, `app.timeutil.as_utc_iso`
- Produces:
  - `app.models.ReminderStatus` (str enum: `pending`, `acked`, `expired`)
  - `app.models.Reminder` table with `id, title, note, due_at, retry_interval_min, max_retries, status, retry_count, last_sent_at, created_at`
  - `app.models.Notification` table with `id, reminder_id, sent_at, acked_at, telegram_message_id`
  - `app.schemas.ReminderCreate`, `ReminderUpdate`, `ReminderRead`, `NotificationRead`, `ReminderDetail`, and `to_read(reminder) -> ReminderRead`, `to_notification_read(n) -> NotificationRead`, `to_detail(reminder, notifications) -> ReminderDetail`
  - `app.db.Database(db_path: str)` with `.engine`, `.create_all()`, `.session()` contextmanager
  - pytest fixtures `db` (in-memory `Database`) and `session`

- [ ] **Step 1: Write the failing test** — `tests/conftest.py`

```python
import pytest

from app.db import Database


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_all()
    return database


@pytest.fixture
def session(db):
    with db.session() as s:
        yield s
```

- [ ] **Step 2: Write the failing test** — `tests/test_db.py`

```python
from datetime import datetime

from sqlmodel import select

from app.models import Notification, Reminder, ReminderStatus
from app.schemas import to_read


def test_reminder_roundtrips_with_defaults(session):
    session.add(Reminder(title="Take pills", due_at=datetime(2026, 8, 12, 9, 0)))
    session.commit()

    stored = session.exec(select(Reminder)).one()
    assert stored.id == 1
    assert stored.title == "Take pills"
    assert stored.note is None
    assert stored.status == "pending"
    assert stored.retry_count == 0
    assert stored.retry_interval_min == 15
    assert stored.max_retries == 4
    assert stored.last_sent_at is None
    assert stored.created_at is not None


def test_status_column_stores_the_enum_value_not_its_name(session):
    session.add(Reminder(title="x", due_at=datetime(2026, 8, 12, 9, 0),
                         status=ReminderStatus.acked.value))
    session.commit()
    raw = session.connection().exec_driver_sql(
        "SELECT status FROM reminders"
    ).scalar_one()
    assert raw == "acked"


def test_notification_links_to_reminder(session):
    reminder = Reminder(title="x", due_at=datetime(2026, 8, 12, 9, 0))
    session.add(reminder)
    session.commit()
    session.add(Notification(reminder_id=reminder.id, sent_at=datetime(2026, 8, 12, 9, 1),
                             telegram_message_id=555))
    session.commit()

    stored = session.exec(select(Notification)).one()
    assert stored.reminder_id == reminder.id
    assert stored.acked_at is None
    assert stored.telegram_message_id == 555


def test_to_read_renders_datetimes_as_utc_iso(session):
    reminder = Reminder(title="x", note="n", due_at=datetime(2026, 8, 12, 9, 0))
    session.add(reminder)
    session.commit()

    read = to_read(reminder)
    assert read.due_at == "2026-08-12T09:00:00+00:00"
    assert read.last_sent_at is None
    assert read.status == "pending"


def test_each_database_instance_is_isolated():
    from app.db import Database

    first, second = Database(":memory:"), Database(":memory:")
    first.create_all()
    second.create_all()
    with first.session() as s:
        s.add(Reminder(title="only in first", due_at=datetime(2026, 8, 12, 9, 0)))
        s.commit()
    with second.session() as s:
        assert s.exec(select(Reminder)).all() == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 4: Write `app/models.py`**

```python
from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel

from app.timeutil import utcnow


class ReminderStatus(str, Enum):
    pending = "pending"
    acked = "acked"
    expired = "expired"


class Reminder(SQLModel, table=True):
    __tablename__ = "reminders"

    id: int | None = Field(default=None, primary_key=True)
    title: str
    note: str | None = Field(default=None)
    due_at: datetime = Field(index=True)
    retry_interval_min: int = Field(default=15)
    max_retries: int = Field(default=4)
    # Plain str column holding the enum *value*. Never a SQLAlchemy Enum type:
    # that validates by member name and silently breaks on value-based writes.
    status: str = Field(default=ReminderStatus.pending.value, index=True)
    retry_count: int = Field(default=0)
    last_sent_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    reminder_id: int = Field(foreign_key="reminders.id", index=True)
    sent_at: datetime = Field(default_factory=utcnow)
    acked_at: datetime | None = Field(default=None)
    # Needed so a plain-text-reply ack can edit the message it is acking.
    telegram_message_id: int | None = Field(default=None)
```

- [ ] **Step 5: Write `app/schemas.py`**

```python
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Notification, Reminder
from app.timeutil import as_utc_iso


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    due_at: datetime
    retry_interval_min: int = Field(default=15, ge=1, le=1440)
    max_retries: int = Field(default=4, ge=1, le=100)


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    due_at: datetime | None = None
    retry_interval_min: int | None = Field(default=None, ge=1, le=1440)
    max_retries: int | None = Field(default=None, ge=1, le=100)


class NotificationRead(BaseModel):
    id: int
    sent_at: str
    acked_at: str | None


class ReminderRead(BaseModel):
    id: int
    title: str
    note: str | None
    due_at: str
    retry_interval_min: int
    max_retries: int
    status: str
    retry_count: int
    last_sent_at: str | None
    created_at: str


class ReminderDetail(ReminderRead):
    notifications: list[NotificationRead]


def to_notification_read(notification: Notification) -> NotificationRead:
    return NotificationRead(
        id=notification.id,
        sent_at=as_utc_iso(notification.sent_at),
        acked_at=as_utc_iso(notification.acked_at),
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
    )


def to_detail(reminder: Reminder, notifications: list[Notification]) -> ReminderDetail:
    return ReminderDetail(
        **to_read(reminder).model_dump(),
        notifications=[to_notification_read(n) for n in notifications],
    )
```

- [ ] **Step 6: Write `app/db.py`**

```python
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# Importing the models module registers the tables on SQLModel.metadata,
# which is what create_all() reads. Without it create_all() is a no-op.
from app import models  # noqa: F401


class Database:
    """Owns the SQLite engine. Instantiated once in lifespan, per-test in tests."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path == ":memory:":
            # StaticPool keeps every session on the same in-memory connection,
            # otherwise each session would get a fresh, empty database.
            self.engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )

    def create_all(self) -> None:
        SQLModel.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = Session(self.engine)
        try:
            yield session
        finally:
            session.close()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS, 15 passed (4 config + 6 timeutil + 5 db)

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/schemas.py app/db.py tests/conftest.py tests/test_db.py pyproject.toml
git commit -m "feat: reminder/notification models, API schemas, and SQLite database"
```

---

### Task 4: The retry/expiry decision (pure function)

This is the heart of the service. It is deliberately pure — no database, no clock, no network — so every branch can be tested by passing values in.

**Files:**
- Create: `app/logic.py`
- Test: `tests/test_logic.py`

**Interfaces:**
- Consumes: `app.models.ReminderStatus`
- Produces: `app.logic.Action` (str enum: `nothing`, `send`, `expire`) and

```python
def decide(*, status: str, due_at: datetime, last_sent_at: datetime | None,
           retry_count: int, retry_interval_min: int, max_retries: int,
           now: datetime) -> Action
```

All datetimes are naive UTC. Keyword-only, so call sites cannot silently transpose arguments.

- [ ] **Step 1: Write the failing test** — `tests/test_logic.py`

```python
from datetime import datetime, timedelta

import pytest

from app.logic import Action, decide

NOW = datetime(2026, 8, 12, 12, 0, 0)


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


@pytest.mark.parametrize("status", ["acked", "expired"])
def test_non_pending_reminders_are_left_alone(status):
    assert call(status=status) == Action.NOTHING


def test_reminder_due_in_the_future_is_not_sent():
    assert call(due_at=NOW + timedelta(minutes=1)) == Action.NOTHING


def test_first_send_happens_once_due():
    assert call(due_at=NOW - timedelta(seconds=1), last_sent_at=None) == Action.SEND


def test_reminder_due_exactly_now_is_sent():
    assert call(due_at=NOW, last_sent_at=None) == Action.SEND


def test_no_resend_before_the_retry_interval_elapses():
    assert call(last_sent_at=NOW - timedelta(minutes=5), retry_count=1) == Action.NOTHING


def test_resend_once_the_interval_has_exactly_elapsed():
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=1) == Action.SEND


def test_resend_while_under_the_send_budget():
    assert call(last_sent_at=NOW - timedelta(minutes=20), retry_count=3,
                max_retries=4) == Action.SEND


def test_expires_once_the_budget_is_spent_and_the_interval_elapsed():
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=4,
                max_retries=4) == Action.EXPIRE


def test_does_not_expire_immediately_after_the_final_send():
    """The spec's pseudocode would expire here, killing the last chance to ack."""
    assert call(last_sent_at=NOW - timedelta(seconds=30), retry_count=4,
                max_retries=4) == Action.NOTHING


def test_custom_interval_is_respected():
    assert call(last_sent_at=NOW - timedelta(minutes=3), retry_count=1,
                retry_interval_min=2) == Action.SEND
    assert call(last_sent_at=NOW - timedelta(minutes=1), retry_count=1,
                retry_interval_min=2) == Action.NOTHING


def test_max_retries_of_one_sends_once_then_expires():
    assert call(last_sent_at=None, retry_count=0, max_retries=1) == Action.SEND
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=1,
                max_retries=1) == Action.EXPIRE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_logic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.logic'`

- [ ] **Step 3: Write `app/logic.py`**

```python
from datetime import datetime, timedelta
from enum import Enum

from app.models import ReminderStatus


class Action(str, Enum):
    NOTHING = "nothing"
    SEND = "send"
    EXPIRE = "expire"


def decide(
    *,
    status: str,
    due_at: datetime,
    last_sent_at: datetime | None,
    retry_count: int,
    retry_interval_min: int,
    max_retries: int,
    now: datetime,
) -> Action:
    """Decide what a single reminder needs right now.

    All datetimes are naive UTC. `retry_count` counts messages already sent,
    so `max_retries` is really a total-send budget (see plan deviation 3).
    """
    if status != ReminderStatus.pending.value:
        return Action.NOTHING

    if due_at > now:
        return Action.NOTHING

    if last_sent_at is None:
        return Action.SEND

    if now - last_sent_at < timedelta(minutes=retry_interval_min):
        # Too soon to nag again — and too soon to give up, since the user
        # still has the rest of this interval to acknowledge the last message.
        return Action.NOTHING

    if retry_count < max_retries:
        return Action.SEND

    return Action.EXPIRE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_logic.py -v`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add app/logic.py tests/test_logic.py
git commit -m "feat: pure retry/expiry decision function"
```

---

### Task 5: Service layer — acking and ack targeting

**Files:**
- Create: `app/service.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `app.models.Reminder`, `app.models.Notification`, `app.models.ReminderStatus`, `app.timeutil.utcnow`
- Produces:
  - `ack_reminder(session: Session, reminder_id: int, *, now: datetime | None = None) -> bool` — returns `True` if it flipped a pending reminder to acked, `False` if unknown or already resolved
  - `find_reply_ack_target(session: Session) -> Reminder | None` — the reminder a bare text reply should ack
  - `latest_notification(session: Session, reminder_id: int) -> Notification | None`
  - `record_send(session: Session, reminder: Reminder, *, now: datetime, message_id: int | None) -> None` — appends a `Notification` and bumps `retry_count`/`last_sent_at` (does not commit)

- [ ] **Step 1: Write the failing test** — `tests/test_service.py`

```python
from datetime import datetime, timedelta

from sqlmodel import select

from app.models import Notification, Reminder, ReminderStatus
from app.service import (
    ack_reminder,
    find_reply_ack_target,
    latest_notification,
    record_send,
)

NOW = datetime(2026, 8, 12, 12, 0, 0)


def make_reminder(session, **overrides) -> Reminder:
    fields = dict(title="t", due_at=NOW - timedelta(hours=1))
    fields.update(overrides)
    reminder = Reminder(**fields)
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def test_ack_marks_the_reminder_acked(session):
    reminder = make_reminder(session)
    assert ack_reminder(session, reminder.id, now=NOW) is True
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.acked.value


def test_ack_stamps_the_latest_notification(session):
    reminder = make_reminder(session)
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW - timedelta(minutes=30)))
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW - timedelta(minutes=15)))
    session.commit()

    ack_reminder(session, reminder.id, now=NOW)

    rows = session.exec(select(Notification).order_by(Notification.sent_at)).all()
    assert rows[0].acked_at is None
    assert rows[1].acked_at == NOW


def test_ack_is_idempotent(session):
    reminder = make_reminder(session)
    assert ack_reminder(session, reminder.id, now=NOW) is True
    assert ack_reminder(session, reminder.id, now=NOW) is False


def test_ack_refuses_an_expired_reminder(session):
    reminder = make_reminder(session, status=ReminderStatus.expired.value)
    assert ack_reminder(session, reminder.id, now=NOW) is False
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.expired.value


def test_ack_of_unknown_id_returns_false(session):
    assert ack_reminder(session, 999, now=NOW) is False


def test_ack_works_with_no_notifications_recorded(session):
    reminder = make_reminder(session)
    assert ack_reminder(session, reminder.id, now=NOW) is True


def test_reply_target_is_the_most_recently_notified_pending_reminder(session):
    make_reminder(session, title="old", last_sent_at=NOW - timedelta(minutes=40))
    newest = make_reminder(session, title="new", last_sent_at=NOW - timedelta(minutes=5))
    make_reminder(session, title="never sent", last_sent_at=None)

    target = find_reply_ack_target(session)
    assert target.id == newest.id


def test_reply_target_ignores_acked_and_expired(session):
    make_reminder(session, title="acked", status=ReminderStatus.acked.value,
                  last_sent_at=NOW - timedelta(minutes=1))
    make_reminder(session, title="expired", status=ReminderStatus.expired.value,
                  last_sent_at=NOW - timedelta(minutes=2))
    pending = make_reminder(session, title="pending", last_sent_at=NOW - timedelta(minutes=30))

    assert find_reply_ack_target(session).id == pending.id


def test_reply_target_is_none_when_nothing_has_been_sent(session):
    make_reminder(session, last_sent_at=None)
    assert find_reply_ack_target(session) is None


def test_record_send_appends_notification_and_bumps_counters(session):
    reminder = make_reminder(session)
    record_send(session, reminder, now=NOW, message_id=42)
    session.commit()

    session.refresh(reminder)
    assert reminder.retry_count == 1
    assert reminder.last_sent_at == NOW

    notification = latest_notification(session, reminder.id)
    assert notification.sent_at == NOW
    assert notification.telegram_message_id == 42
    assert notification.acked_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.service'`

- [ ] **Step 3: Write `app/service.py`**

```python
from datetime import datetime

from sqlmodel import Session, select

from app.models import Notification, Reminder, ReminderStatus
from app.timeutil import utcnow


def latest_notification(session: Session, reminder_id: int) -> Notification | None:
    """The most recent notification sent for a reminder, if any."""
    return session.exec(
        select(Notification)
        .where(Notification.reminder_id == reminder_id)
        .order_by(Notification.sent_at.desc(), Notification.id.desc())
    ).first()


def ack_reminder(session: Session, reminder_id: int, *, now: datetime | None = None) -> bool:
    """Mark a pending reminder acknowledged.

    Returns False (and changes nothing) if the reminder is unknown or has
    already been acked or expired, which makes double-taps on the inline
    button harmless.
    """
    now = now or utcnow()
    reminder = session.get(Reminder, reminder_id)
    if reminder is None or reminder.status != ReminderStatus.pending.value:
        return False

    reminder.status = ReminderStatus.acked.value
    session.add(reminder)

    notification = latest_notification(session, reminder_id)
    if notification is not None and notification.acked_at is None:
        notification.acked_at = now
        session.add(notification)

    session.commit()
    return True


def find_reply_ack_target(session: Session) -> Reminder | None:
    """The reminder that a bare text reply should acknowledge.

    Defined as the pending reminder most recently nagged about. Reminders that
    have never been sent are excluded — the user cannot be replying to them.
    """
    return session.exec(
        select(Reminder)
        .where(
            Reminder.status == ReminderStatus.pending.value,
            Reminder.last_sent_at.is_not(None),
        )
        .order_by(Reminder.last_sent_at.desc(), Reminder.id.desc())
    ).first()


def record_send(
    session: Session,
    reminder: Reminder,
    *,
    now: datetime,
    message_id: int | None,
) -> None:
    """Log a delivered notification and advance the reminder's send counters.

    Does not commit — the caller owns the transaction boundary.
    """
    session.add(
        Notification(
            reminder_id=reminder.id,
            sent_at=now,
            telegram_message_id=message_id,
        )
    )
    reminder.retry_count += 1
    reminder.last_sent_at = now
    session.add(reminder)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_service.py -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat: ack and send-recording service layer"
```

---

### Task 6: REST API

**Files:**
- Create: `app/routers/reminders.py`, `app/main.py`
- Modify: `tests/conftest.py` (add a `client` fixture)
- Test: `tests/test_api.py`

`app/main.py` is created minimally here (app + router + db on `app.state`) and finished in Task 9 when the bot and scheduler get wired in.

**Interfaces:**
- Consumes: `app.db.Database`, `app.models.*`, `app.schemas.*`, `app.service.latest_notification`, `app.timeutil.to_utc_naive`
- Produces:
  - `app.routers.reminders.router` — `POST /api/reminders`, `GET /api/reminders?status=`, `GET /api/reminders/{id}`, `PATCH /api/reminders/{id}`, `DELETE /api/reminders/{id}`
  - `app.main.create_app(db: Database | None = None) -> FastAPI`

The API is mounted under `/api` so that `StaticFiles` can own `/` without shadowing it.

- [ ] **Step 1: Add the `client` fixture to `tests/conftest.py`**

```python
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_all()
    return database


@pytest.fixture
def session(db):
    with db.session() as s:
        yield s


@pytest.fixture
def client(db) -> TestClient:
    # create_app(db) skips the lifespan wiring of bot + scheduler, so API
    # tests exercise the routes alone.
    with TestClient(create_app(db=db)) as test_client:
        yield test_client
```

- [ ] **Step 2: Write the failing test** — `tests/test_api.py`

```python
def create(client, **overrides):
    payload = {
        "title": "Take pills",
        "note": "the blue ones",
        "due_at": "2026-08-12T09:00:00+00:00",
        "retry_interval_min": 15,
        "max_retries": 4,
    }
    payload.update(overrides)
    return client.post("/api/reminders", json=payload)


def test_create_returns_the_stored_reminder(client):
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["title"] == "Take pills"
    assert body["note"] == "the blue ones"
    assert body["status"] == "pending"
    assert body["retry_count"] == 0
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_create_normalises_a_non_utc_due_at_to_utc(client):
    body = create(client, due_at="2026-08-12T14:00:00+05:00").json()
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_create_applies_defaults_for_optional_fields(client):
    response = client.post("/api/reminders",
                           json={"title": "bare", "due_at": "2026-08-12T09:00:00+00:00"})
    body = response.json()
    assert body["retry_interval_min"] == 15
    assert body["max_retries"] == 4
    assert body["note"] is None


def test_create_rejects_an_empty_title(client):
    assert create(client, title="").status_code == 422


def test_create_rejects_a_zero_retry_interval(client):
    assert create(client, retry_interval_min=0).status_code == 422


def test_list_returns_newest_due_first(client):
    create(client, title="later", due_at="2026-08-12T18:00:00+00:00")
    create(client, title="sooner", due_at="2026-08-12T09:00:00+00:00")
    titles = [r["title"] for r in client.get("/api/reminders").json()]
    assert titles == ["sooner", "later"]


def test_list_filters_by_status(client):
    create(client, title="a")
    create(client, title="b")
    client.patch("/api/reminders/2", json={"title": "b2"})

    pending = client.get("/api/reminders", params={"status": "pending"}).json()
    assert len(pending) == 2
    assert client.get("/api/reminders", params={"status": "acked"}).json() == []


def test_list_rejects_an_unknown_status(client):
    assert client.get("/api/reminders", params={"status": "bogus"}).status_code == 422


def test_detail_includes_notification_history(client):
    create(client)
    body = client.get("/api/reminders/1").json()
    assert body["title"] == "Take pills"
    assert body["notifications"] == []


def test_detail_of_unknown_id_is_404(client):
    assert client.get("/api/reminders/999").status_code == 404


def test_patch_updates_only_the_supplied_fields(client):
    create(client)
    body = client.patch("/api/reminders/1", json={"title": "New title"}).json()
    assert body["title"] == "New title"
    assert body["note"] == "the blue ones"
    assert body["retry_interval_min"] == 15


def test_patch_normalises_due_at_to_utc(client):
    create(client)
    body = client.patch("/api/reminders/1",
                        json={"due_at": "2026-08-12T14:00:00+05:00"}).json()
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_patch_is_rejected_once_the_reminder_is_resolved(client, db):
    from app.models import Reminder, ReminderStatus

    create(client)
    with db.session() as s:
        reminder = s.get(Reminder, 1)
        reminder.status = ReminderStatus.acked.value
        s.add(reminder)
        s.commit()

    response = client.patch("/api/reminders/1", json={"title": "nope"})
    assert response.status_code == 409


def test_patch_of_unknown_id_is_404(client):
    assert client.patch("/api/reminders/999", json={"title": "x"}).status_code == 404


def test_delete_removes_the_reminder_and_its_notifications(client, db):
    from app.models import Notification

    create(client)
    with db.session() as s:
        s.add(Notification(reminder_id=1))
        s.commit()

    assert client.delete("/api/reminders/1").status_code == 204
    assert client.get("/api/reminders/1").status_code == 404
    with db.session() as s:
        from sqlmodel import select
        assert s.exec(select(Notification)).all() == []


def test_delete_of_unknown_id_is_404(client):
    assert client.delete("/api/reminders/999").status_code == 404


def test_healthz_reports_ok(client):
    assert client.get("/api/healthz").json()["status"] == "ok"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 4: Write `app/routers/reminders.py`**

```python
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from app.db import Database
from app.models import Notification, Reminder, ReminderStatus
from app.schemas import (
    ReminderCreate,
    ReminderDetail,
    ReminderRead,
    ReminderUpdate,
    to_detail,
    to_read,
)
from app.timeutil import to_utc_naive

router = APIRouter(prefix="/api", tags=["reminders"])


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.db
    with database.session() as session:
        yield session


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/reminders", response_model=ReminderRead, status_code=201)
def create_reminder(payload: ReminderCreate, session: Session = Depends(get_session)):
    reminder = Reminder(
        title=payload.title,
        note=payload.note,
        due_at=to_utc_naive(payload.due_at),
        retry_interval_min=payload.retry_interval_min,
        max_retries=payload.max_retries,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return to_read(reminder)


@router.get("/reminders", response_model=list[ReminderRead])
def list_reminders(
    status: ReminderStatus | None = None,
    session: Session = Depends(get_session),
):
    statement = select(Reminder)
    if status is not None:
        statement = statement.where(Reminder.status == status.value)
    statement = statement.order_by(Reminder.due_at, Reminder.id)
    return [to_read(r) for r in session.exec(statement).all()]


def _get_or_404(session: Session, reminder_id: int) -> Reminder:
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return reminder


@router.get("/reminders/{reminder_id}", response_model=ReminderDetail)
def get_reminder(reminder_id: int, session: Session = Depends(get_session)):
    reminder = _get_or_404(session, reminder_id)
    notifications = session.exec(
        select(Notification)
        .where(Notification.reminder_id == reminder_id)
        .order_by(Notification.sent_at, Notification.id)
    ).all()
    return to_detail(reminder, list(notifications))


@router.patch("/reminders/{reminder_id}", response_model=ReminderRead)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    session: Session = Depends(get_session),
):
    reminder = _get_or_404(session, reminder_id)
    if reminder.status != ReminderStatus.pending.value:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit a reminder that is already {reminder.status}",
        )

    changes = payload.model_dump(exclude_unset=True)
    if "due_at" in changes and changes["due_at"] is not None:
        changes["due_at"] = to_utc_naive(changes["due_at"])
    for field, value in changes.items():
        setattr(reminder, field, value)

    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return to_read(reminder)


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: int, session: Session = Depends(get_session)):
    reminder = _get_or_404(session, reminder_id)
    for notification in session.exec(
        select(Notification).where(Notification.reminder_id == reminder_id)
    ).all():
        session.delete(notification)
    session.delete(reminder)
    session.commit()
    return Response(status_code=204)
```

`exclude_unset=True` is what makes PATCH a genuine partial update: a field the client omitted is absent from `changes`, while an explicitly sent `"note": null` is present and clears the value.

- [ ] **Step 5: Write a minimal `app/main.py`**

Task 9 replaces the `lifespan` with the full bot + scheduler wiring; this version only needs to serve the API.

```python
import logging

from fastapi import FastAPI

from app.config import load_settings
from app.db import Database
from app.routers import reminders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("reminder")


def create_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app.

    Passing `db` (as tests do) skips runtime wiring entirely.
    """
    app = FastAPI(title="Reminder Service")
    settings = load_settings()
    app.state.settings = settings
    app.state.db = db or Database(settings.db_path)
    app.state.db.create_all()
    app.include_router(reminders.router)
    return app


app = create_app()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: PASS, 17 passed

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS, 54 passed

- [ ] **Step 8: Commit**

```bash
git add app/routers/reminders.py app/main.py tests/conftest.py tests/test_api.py
git commit -m "feat: reminder CRUD API"
```

---

### Task 7: Scheduler tick

**Files:**
- Create: `app/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `app.db.Database`, `app.logic.Action`, `app.logic.decide`, `app.service.record_send`, `app.timeutil.utcnow`
- Produces:
  - `Sender = Callable[[Reminder], Awaitable[int | None]]`
  - `async def tick(db: Database, sender: Sender, *, now_fn: Callable[[], datetime] = utcnow) -> None`
  - `async def log_sender(reminder: Reminder) -> None` — the stand-in used when Telegram is disabled
  - `build_scheduler(db, sender, tick_seconds) -> AsyncIOScheduler`

- [ ] **Step 1: Write the failing test** — `tests/test_scheduler.py`

```python
from datetime import datetime, timedelta

from sqlmodel import select

from app.models import Notification, Reminder, ReminderStatus
from app.scheduler import build_scheduler, tick

NOW = datetime(2026, 8, 12, 12, 0, 0)


class FakeSender:
    """Records what it was asked to send; hands back fake Telegram message ids."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.sent: list[int] = []
        self.fail_on = fail_on or set()
        self._next_message_id = 1000

    async def __call__(self, reminder: Reminder) -> int:
        if reminder.id in self.fail_on:
            raise RuntimeError("telegram is down")
        self.sent.append(reminder.id)
        self._next_message_id += 1
        return self._next_message_id


def add(db, **overrides) -> int:
    fields = dict(title="t", due_at=NOW - timedelta(hours=1))
    fields.update(overrides)
    with db.session() as s:
        reminder = Reminder(**fields)
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        return reminder.id


def load(db, reminder_id: int) -> Reminder:
    with db.session() as s:
        return s.get(Reminder, reminder_id)


async def test_sends_a_due_reminder_and_records_it(db):
    reminder_id = add(db)
    sender = FakeSender()

    await tick(db, sender, now_fn=lambda: NOW)

    assert sender.sent == [reminder_id]
    reminder = load(db, reminder_id)
    assert reminder.retry_count == 1
    assert reminder.last_sent_at == NOW
    assert reminder.status == ReminderStatus.pending.value

    with db.session() as s:
        notification = s.exec(select(Notification)).one()
    assert notification.reminder_id == reminder_id
    assert notification.sent_at == NOW
    assert notification.telegram_message_id == 1001


async def test_does_not_send_a_reminder_that_is_not_due(db):
    add(db, due_at=NOW + timedelta(hours=1))
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_does_not_resend_within_the_retry_interval(db):
    add(db, last_sent_at=NOW - timedelta(minutes=5), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_resends_after_the_retry_interval(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 2


async def test_expires_after_the_send_budget_is_spent(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15),
                      retry_count=4, max_retries=4)
    sender = FakeSender()

    await tick(db, sender, now_fn=lambda: NOW)

    assert sender.sent == []
    assert load(db, reminder_id).status == ReminderStatus.expired.value


async def test_ignores_acked_reminders(db):
    add(db, status=ReminderStatus.acked.value)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_a_send_failure_does_not_advance_counters_or_stop_the_tick(db):
    broken_id = add(db, title="broken")
    healthy_id = add(db, title="healthy")
    sender = FakeSender(fail_on={broken_id})

    await tick(db, sender, now_fn=lambda: NOW)

    assert sender.sent == [healthy_id]
    broken = load(db, broken_id)
    assert broken.retry_count == 0
    assert broken.last_sent_at is None
    assert broken.status == ReminderStatus.pending.value
    assert load(db, healthy_id).retry_count == 1


async def test_a_failed_send_is_retried_on_the_next_tick(db):
    reminder_id = add(db)
    await tick(db, FakeSender(fail_on={reminder_id}), now_fn=lambda: NOW)

    recovered = FakeSender()
    await tick(db, recovered, now_fn=lambda: NOW)

    assert recovered.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 1


async def test_tick_with_no_reminders_is_a_no_op(db):
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


def test_build_scheduler_registers_a_single_non_overlapping_job(db):
    scheduler = build_scheduler(db, FakeSender(), tick_seconds=30)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "reminder-tick"
    assert jobs[0].max_instances == 1
    assert jobs[0].trigger.interval.total_seconds() == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.scheduler'`

- [ ] **Step 3: Write `app/scheduler.py`**

```python
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.db import Database
from app.logic import Action, decide
from app.models import Reminder, ReminderStatus
from app.service import record_send
from app.timeutil import utcnow

logger = logging.getLogger("reminder.scheduler")

Sender = Callable[[Reminder], Awaitable[int | None]]


async def log_sender(reminder: Reminder) -> None:
    """Stand-in sender used when Telegram is not configured."""
    logger.info(
        "[no telegram] would send reminder %s: %s", reminder.id, reminder.title
    )
    return None


async def tick(
    db: Database,
    sender: Sender,
    *,
    now_fn: Callable[[], datetime] = utcnow,
) -> None:
    """One scheduler pass: send what is due, expire what is spent.

    A failing send is logged and skipped without touching that reminder's
    counters, so the next tick retries it rather than burning an attempt.
    One bad reminder never blocks the others.
    """
    now = now_fn()
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
                reminder.status = ReminderStatus.expired.value
                session.add(reminder)
                logger.info(
                    "expired reminder %s (%s) after %s attempts",
                    reminder.id,
                    reminder.title,
                    reminder.retry_count,
                )

        session.commit()


def build_scheduler(db: Database, sender: Sender, tick_seconds: int) -> AsyncIOScheduler:
    """An AsyncIOScheduler that runs `tick` on the app's own event loop.

    max_instances=1 plus coalesce=True mean a slow tick can never overlap
    itself or replay a backlog of missed runs — either would double-send.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger="interval",
        seconds=tick_seconds,
        args=[db, sender],
        id="reminder-tick",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler tick with injectable sender"
```

---

### Task 8: Telegram bot

**Files:**
- Create: `app/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `app.db.Database`, `app.service.ack_reminder`, `app.service.find_reply_ack_target`, `app.service.latest_notification`, `app.timeutil.utcnow`
- Produces:
  - `async def send_reminder_message(bot, chat_id: int, reminder: Reminder) -> int` — returns the Telegram message id
  - `async def handle_callback(update, context, *, db, chat_id) -> None`
  - `async def handle_text(update, context, *, db, chat_id) -> None`
  - `build_application(token: str, chat_id: int, db: Database) -> Application`
  - `CALLBACK_PREFIX = "ack:"`

Handlers touch only a handful of attributes on `update`/`context`, which is what lets the tests drive them with lightweight fakes instead of real (immutable, network-bound) Telegram objects.

- [ ] **Step 1: Write the failing test** — `tests/test_bot.py`

```python
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import handle_callback, handle_text, send_reminder_message
from app.models import Notification, Reminder, ReminderStatus

NOW = datetime(2026, 8, 12, 12, 0, 0)
CHAT_ID = 987654321


def add(db, **overrides) -> int:
    fields = dict(title="Take pills", due_at=NOW - timedelta(hours=1))
    fields.update(overrides)
    with db.session() as s:
        reminder = Reminder(**fields)
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        return reminder.id


def load(db, reminder_id: int) -> Reminder:
    with db.session() as s:
        return s.get(Reminder, reminder_id)


def fake_callback_update(data: str, chat_id: int = CHAT_ID):
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat_id=chat_id, text="⏰ Take pills"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query)


def fake_text_update(text: str, chat_id: int = CHAT_ID):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def fake_context():
    return SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))


async def test_send_builds_a_message_with_a_done_button():
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=4242)))
    reminder = Reminder(id=7, title="Take pills", note="the blue ones",
                        due_at=NOW, retry_count=1, max_retries=4)

    message_id = await send_reminder_message(bot, CHAT_ID, reminder)

    assert message_id == 4242
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert "Take pills" in kwargs["text"]
    assert "the blue ones" in kwargs["text"]
    assert "2/4" in kwargs["text"]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "ack:7"
    assert "Done" in button.text
    # Plain text: a title containing *markdown* must not need escaping.
    assert "parse_mode" not in kwargs


async def test_send_omits_the_note_line_when_there_is_no_note():
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=1)))
    reminder = Reminder(id=1, title="Bare", note=None, due_at=NOW,
                        retry_count=0, max_retries=4)
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert bot.send_message.await_args.kwargs["text"].count("\n\n") == 1


async def test_button_tap_acks_the_reminder(db):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}")

    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    update.callback_query.answer.assert_awaited()
    edited = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert "Done" in edited


async def test_button_tap_from_an_unauthorised_chat_changes_nothing(db):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}", chat_id=111222333)

    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_second_button_tap_is_harmless(db):
    reminder_id = add(db)
    await handle_callback(fake_callback_update(f"ack:{reminder_id}"),
                          fake_context(), db=db, chat_id=CHAT_ID)

    update = fake_callback_update(f"ack:{reminder_id}")
    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    assert "already" in update.callback_query.edit_message_text.await_args.kwargs["text"]


async def test_malformed_callback_data_is_ignored(db):
    update = fake_callback_update("garbage")
    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_text_reply_acks_the_most_recently_notified_reminder(db):
    add(db, title="old", last_sent_at=NOW - timedelta(minutes=40))
    newest_id = add(db, title="new", last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done")

    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, newest_id).status == ReminderStatus.acked.value
    assert "new" in update.message.reply_text.await_args.args[0]


async def test_text_reply_edits_the_original_message_when_known(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    with db.session() as s:
        s.add(Notification(reminder_id=reminder_id, sent_at=NOW - timedelta(minutes=5),
                           telegram_message_id=555))
        s.commit()
    context = fake_context()

    await handle_text(fake_text_update("done"), context, db=db, chat_id=CHAT_ID)

    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["message_id"] == 555
    assert "Done" in kwargs["text"]


async def test_text_reply_with_nothing_pending_says_so(db):
    update = fake_text_update("hello?")
    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)
    assert "Nothing pending" in update.message.reply_text.await_args.args[0]


async def test_text_reply_ignores_reminders_never_sent(db):
    reminder_id = add(db, last_sent_at=None)
    await handle_text(fake_text_update("done"), fake_context(), db=db, chat_id=CHAT_ID)
    assert load(db, reminder_id).status == ReminderStatus.pending.value


async def test_text_from_an_unauthorised_chat_is_ignored(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done", chat_id=111222333)

    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.message.reply_text.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bot'`

- [ ] **Step 3: Write `app/bot.py`**

```python
import logging
from functools import partial

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from app.db import Database
from app.models import Reminder
from app.service import ack_reminder, find_reply_ack_target, latest_notification
from app.timeutil import as_utc_iso, utcnow

logger = logging.getLogger("reminder.bot")

CALLBACK_PREFIX = "ack:"


def _compose(reminder: Reminder) -> str:
    """Plain-text message body. No parse_mode, so titles never need escaping."""
    lines = [f"⏰ {reminder.title}"]
    if reminder.note:
        lines.append(reminder.note)
    lines.append(
        f"Due {as_utc_iso(reminder.due_at)} · "
        f"attempt {reminder.retry_count + 1}/{reminder.max_retries}"
    )
    return "\n\n".join(lines)


async def send_reminder_message(bot, chat_id: int, reminder: Reminder) -> int:
    """Send one nag with a Done button. Returns the Telegram message id."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done", callback_data=f"{CALLBACK_PREFIX}{reminder.id}")]]
    )
    message = await bot.send_message(
        chat_id=chat_id,
        text=_compose(reminder),
        reply_markup=keyboard,
    )
    return message.message_id


async def handle_callback(update, context, *, db: Database, chat_id: int) -> None:
    """Inline '✅ Done' button tap."""
    query = update.callback_query
    if query.message.chat_id != chat_id:
        logger.warning("ignoring callback from unauthorised chat %s", query.message.chat_id)
        await query.answer("Not authorised.")
        return

    data = query.data or ""
    if not data.startswith(CALLBACK_PREFIX):
        await query.answer()
        return
    try:
        reminder_id = int(data[len(CALLBACK_PREFIX):])
    except ValueError:
        await query.answer()
        return

    await query.answer()
    now = utcnow()
    with db.session() as session:
        acked = ack_reminder(session, reminder_id, now=now)

    original = query.message.text or ""
    suffix = f"✅ Done at {now:%H:%M} UTC" if acked else "(already resolved)"
    # Passing no reply_markup drops the button, so the message cannot be re-tapped.
    await query.edit_message_text(text=f"{original}\n\n{suffix}")


async def handle_text(update, context, *, db: Database, chat_id: int) -> None:
    """Any plain-text reply counts as an ack (spec §5) — no intent parsing."""
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
        ack_reminder(session, target.id, now=now)

    await update.message.reply_text(f"✅ Marked “{title}” done.")

    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏰ {title}\n\n✅ Done at {now:%H:%M} UTC",
            )
        except Exception:
            # The original may be too old to edit; the ack itself already stuck.
            logger.info("could not edit message %s for reminder ack", message_id)


def build_application(token: str, chat_id: int, db: Database) -> Application:
    """Wire the long-polling Telegram application.

    The chat filter is a second guard in front of the per-handler check, so an
    unauthorised chat is dropped before any handler body runs.
    """
    application = Application.builder().token(token).build()
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback, db=db, chat_id=chat_id))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=chat_id),
            partial(handle_text, db=db, chat_id=chat_id),
        )
    )
    return application
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot.py -v`
Expected: PASS, 11 passed

- [ ] **Step 5: Commit**

```bash
git add app/bot.py tests/test_bot.py
git commit -m "feat: telegram bot with button and text-reply acks"
```

---

### Task 9: Wire bot + scheduler into the app lifespan

**Files:**
- Modify: `app/main.py` (replace the whole file)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `app.bot.build_application`, `app.bot.send_reminder_message`, `app.scheduler.build_scheduler`, `app.scheduler.log_sender`, `app.config.load_settings`
- Produces: `app.main.create_app(db=None) -> FastAPI` (unchanged signature) with a `lifespan` that starts the scheduler always and the bot only when configured

- [ ] **Step 1: Write the failing test** — `tests/test_main.py`

```python
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app


def test_app_boots_without_telegram_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").json()["status"] == "ok"


def test_scheduler_runs_even_with_telegram_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    app = create_app()
    with TestClient(app):
        assert app.state.scheduler.running is True
        assert app.state.tg is None


def test_static_dashboard_is_served_at_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_injected_db_still_bypasses_runtime_wiring():
    db = Database(":memory:")
    db.create_all()
    app = create_app(db=db)
    with TestClient(app) as client:
        assert client.get("/api/healthz").status_code == 200
    assert app.state.db is db
```

`test_static_dashboard_is_served_at_root` will only pass once Task 10 creates `static/index.html`. Create the placeholder file now (`echo '<!doctype html><title>Reminder Service</title>' > static/index.html`) so this task is green on its own; Task 10 replaces it with the real dashboard.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: FAIL — `AttributeError: 'State' object has no attribute 'scheduler'`

- [ ] **Step 3: Rewrite `app/main.py`**

```python
import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bot import build_application, send_reminder_message
from app.config import load_settings
from app.db import Database
from app.routers import reminders
from app.scheduler import build_scheduler, log_sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("reminder")

# Load .env for local (non-Docker) runs. Compose supplies the environment
# itself via env_file, and load_dotenv never overrides an already-set variable,
# so this is a no-op in the container. Deliberately not in config.py: that
# would pull a real .env into the config tests.
load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    db: Database = app.state.db

    if settings.bot_enabled:
        telegram_app = build_application(settings.bot_token, settings.chat_id, db)
        await telegram_app.initialize()
        await telegram_app.start()
        # drop_pending_updates avoids replaying stale taps from while we were down.
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        sender = partial(send_reminder_message, telegram_app.bot, settings.chat_id)
        app.state.tg = telegram_app
        logger.info("telegram bot polling; authorised chat id %s", settings.chat_id)
    else:
        app.state.tg = None
        sender = log_sender
        logger.warning(
            "BOT_TOKEN/CHAT_ID not set — Telegram disabled; "
            "reminders will be logged instead of sent"
        )

    scheduler = build_scheduler(db, sender, settings.tick_seconds)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("scheduler started, ticking every %ss", settings.tick_seconds)

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if app.state.tg is not None:
            await app.state.tg.updater.stop()
            await app.state.tg.stop()
            await app.state.tg.shutdown()
        logger.info("shutdown complete")


def create_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app.

    `db` is injected by tests; production passes nothing and gets a file-backed
    database at `settings.db_path`.
    """
    settings = load_settings()
    app = FastAPI(title="Reminder Service", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db or Database(settings.db_path)
    app.state.db.create_all()

    app.include_router(reminders.router)
    # Mounted last: StaticFiles owns "/" and would otherwise shadow /api.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
```

Note that `create_app(db=...)` no longer bypasses the lifespan — the `client` fixture's `TestClient` context manager runs it, which is fine: with no `BOT_TOKEN` in the test environment the bot is skipped and the scheduler ticks harmlessly against an empty in-memory DB.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS, 79 passed

If `tests/test_api.py` now fails because a `BOT_TOKEN` leaked in from a real `.env`, add this to `tests/conftest.py` to guarantee a clean environment for every test:

```python
@pytest.fixture(autouse=True)
def clean_bot_env(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
```

- [ ] **Step 5: Boot the app for real and check it serves**

```bash
cd /home/redji/reminder-service
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 &
sleep 3
curl -s http://127.0.0.1:8765/api/healthz
curl -s -X POST http://127.0.0.1:8765/api/reminders \
  -H 'content-type: application/json' \
  -d '{"title":"smoke test","due_at":"2026-08-12T09:00:00+00:00","retry_interval_min":1,"max_retries":2}'
sleep 35
curl -s http://127.0.0.1:8765/api/reminders
kill %1
```

Expected: `{"status":"ok"}`; the POST returns the reminder with `retry_count` 0; after the tick the GET shows `retry_count` 1 and a non-null `last_sent_at`, and the uvicorn log contains a `[no telegram] would send reminder 1` line.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py tests/conftest.py static/index.html
git commit -m "feat: wire bot and scheduler into the application lifespan"
```

---

### Task 10: Dashboard

**Files:**
- Create: `static/index.html` (replaces the Task 9 placeholder), `static/app.js`

No unit tests — this is verified by loading it in a browser. The API it drives is already covered by Task 6.

**Interfaces:**
- Consumes: `GET/POST/PATCH/DELETE /api/reminders`
- Produces: nothing other tasks depend on

- [ ] **Step 1: Write `static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reminders</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --text: #16181d; --muted: #6b7280;
    --line: #e3e6ea; --accent: #2f6fed; --pending: #b45309; --acked: #15803d;
    --expired: #9ca3af; --danger: #b91c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --panel: #1c1f26; --text: #e9ecf1; --muted: #9aa3b2;
      --line: #2a2f39; --accent: #5b8dff; --pending: #f0b429; --acked: #4ade80;
      --expired: #6b7280; --danger: #f87171;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.5rem 1rem 4rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 1.25rem; }
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
  button.primary {
    background: var(--accent); border-color: var(--accent); color: #fff;
    cursor: pointer; font-weight: 600;
  }
  button.primary:hover { filter: brightness(1.08); }
  .filters { display: flex; gap: .5rem; margin-bottom: .75rem; flex-wrap: wrap; }
  .filters button { width: auto; cursor: pointer; padding: .35rem .75rem; font-size: .85rem; }
  .filters button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-left-width: 4px;
    border-radius: 8px; padding: .75rem .9rem; margin-bottom: .6rem;
  }
  .card.pending { border-left-color: var(--pending); }
  .card.acked { border-left-color: var(--acked); }
  .card.expired { border-left-color: var(--expired); }
  .card-head { display: flex; justify-content: space-between; align-items: baseline; gap: .75rem; }
  .title { font-weight: 600; }
  .badge { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .note { color: var(--muted); margin: .3rem 0 0; white-space: pre-wrap; }
  .meta { color: var(--muted); font-size: .8rem; margin-top: .45rem; display: flex;
          gap: .9rem; flex-wrap: wrap; align-items: center; }
  .meta button { width: auto; padding: .15rem .5rem; font-size: .75rem; cursor: pointer;
                 color: var(--danger); border-color: transparent; background: transparent; }
  .meta button:hover { border-color: var(--danger); }
  .empty { color: var(--muted); padding: 1rem 0; }
  #error { color: var(--danger); min-height: 1.2rem; font-size: .85rem; }
</style>
</head>
<body>
<main>
  <h1>Reminders</h1>

  <form class="panel" id="create-form">
    <div class="field">
      <label for="title">Title</label>
      <input id="title" name="title" required maxlength="200" placeholder="Take the bins out">
    </div>
    <div class="field">
      <label for="note">Note (optional)</label>
      <textarea id="note" name="note" maxlength="2000"></textarea>
    </div>
    <div class="row">
      <div class="field">
        <label for="due_at">Due (your local time)</label>
        <input id="due_at" name="due_at" type="datetime-local" required>
      </div>
      <div class="field">
        <label for="retry_interval_min">Retry every (min)</label>
        <input id="retry_interval_min" name="retry_interval_min" type="number" min="1" max="1440" value="15">
      </div>
      <div class="field">
        <label for="max_retries">Max sends</label>
        <input id="max_retries" name="max_retries" type="number" min="1" max="100" value="4">
      </div>
    </div>
    <button class="primary" type="submit">Add reminder</button>
    <p id="error" role="alert"></p>
  </form>

  <div class="filters" id="filters">
    <button data-status="all" aria-pressed="true">All</button>
    <button data-status="pending" aria-pressed="false">Pending</button>
    <button data-status="acked" aria-pressed="false">Done</button>
    <button data-status="expired" aria-pressed="false">Expired</button>
    <button id="refresh" aria-pressed="false">Refresh</button>
  </div>

  <div id="list"></div>
</main>
<script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write `static/app.js`**

```javascript
const API = "/api/reminders";
const POLL_MS = 10000;

let activeFilter = "all";

/** Format a datetime-local input value from a Date, in the browser's local zone. */
function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
         `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Render a UTC ISO string from the API as local time. */
function formatLocal(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function showError(message) {
  document.getElementById("error").textContent = message || "";
}

async function loadReminders() {
  const url = activeFilter === "all" ? API : `${API}?status=${activeFilter}`;
  const response = await fetch(url);
  if (!response.ok) {
    showError("Could not load reminders.");
    return;
  }
  render(await response.json());
}

function render(reminders) {
  const list = document.getElementById("list");
  if (reminders.length === 0) {
    list.innerHTML = `<p class="empty">Nothing here.</p>`;
    return;
  }
  list.innerHTML = "";
  for (const reminder of reminders) {
    const card = document.createElement("div");
    card.className = `card ${reminder.status}`;

    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = reminder.title;           // textContent, never innerHTML
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = reminder.status;
    head.append(title, badge);
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
      spanText(`due ${formatLocal(reminder.due_at)}`),
      spanText(`sent ${reminder.retry_count}/${reminder.max_retries}`),
      spanText(`last ${formatLocal(reminder.last_sent_at)}`),
      spanText(`every ${reminder.retry_interval_min}m`),
    );

    const remove = document.createElement("button");
    remove.textContent = "delete";
    remove.addEventListener("click", () => deleteReminder(reminder.id, reminder.title));
    meta.append(remove);

    card.append(meta);
    list.append(card);
  }
}

function spanText(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

async function deleteReminder(id, title) {
  if (!confirm(`Delete “${title}”?`)) return;
  const response = await fetch(`${API}/${id}`, { method: "DELETE" });
  if (!response.ok) {
    showError("Delete failed.");
    return;
  }
  loadReminders();
}

async function submitForm(event) {
  event.preventDefault();
  showError("");
  const form = event.target;
  const dueLocal = form.due_at.value;
  if (!dueLocal) {
    showError("Pick a due time.");
    return;
  }
  const payload = {
    title: form.title.value.trim(),
    note: form.note.value.trim() || null,
    // The input is local time; toISOString converts it to the UTC the API wants.
    due_at: new Date(dueLocal).toISOString(),
    retry_interval_min: Number(form.retry_interval_min.value),
    max_retries: Number(form.max_retries.value),
  };

  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    showError(detail ? JSON.stringify(detail.detail) : "Could not create reminder.");
    return;
  }
  form.reset();
  resetDefaults();
  loadReminders();
}

function resetDefaults() {
  const inFifteen = new Date(Date.now() + 15 * 60 * 1000);
  document.getElementById("due_at").value = toLocalInputValue(inFifteen);
  document.getElementById("retry_interval_min").value = 15;
  document.getElementById("max_retries").value = 4;
}

function wireFilters() {
  document.getElementById("filters").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "refresh") {
      loadReminders();
      return;
    }
    activeFilter = button.dataset.status;
    for (const other of document.querySelectorAll("#filters button[data-status]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    loadReminders();
  });
}

document.getElementById("create-form").addEventListener("submit", submitForm);
wireFilters();
resetDefaults();
loadReminders();
setInterval(loadReminders, POLL_MS);
```

`textContent` is used everywhere rather than `innerHTML` so a reminder titled `<img onerror=...>` renders as literal text.

- [ ] **Step 3: Verify in a browser**

```bash
cd /home/redji/reminder-service
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765 &
sleep 3
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/app.js
```

Expected: `200` twice. Then open `http://<this-host>:8765/` and confirm by hand:
1. The due field is pre-filled with a time ~15 minutes from now, in your local zone.
2. Adding a reminder makes a card appear without a manual refresh.
3. The card's "due" time matches what you typed (this is the UTC round-trip working).
4. The filter buttons switch the list and highlight correctly.
5. Delete asks for confirmation and removes the card.

Stop the server with `kill %1` when done.

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/app.js
git commit -m "feat: dashboard for creating and tracking reminders"
```

---

### Task 11: Docker packaging

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `data/.gitkeep`

**Interfaces:**
- Consumes: everything above
- Produces: a `reminder-service` container listening on host port 8765

- [ ] **Step 1: Write `.dockerignore`**

```
.venv/
.git/
.pytest_cache/
__pycache__/
data/
docs/
tests/
*.md
.env
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/reminders.db

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data

EXPOSE 8000

# --workers 1 is mandatory: a second worker means a second scheduler and a
# second polling loop, i.e. duplicate nags and a Telegram getUpdates conflict.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

- [ ] **Step 3: Write `docker-compose.yml`**

```yaml
services:
  reminder-service:
    build: .
    container_name: reminder-service
    restart: unless-stopped
    env_file: .env
    environment:
      DB_PATH: /data/reminders.db
      TZ: UTC
    volumes:
      - ./data:/data
    ports:
      # Host 8765 — port 8000 on this host is already taken by swingbot.
      - "8765:8000"
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

- [ ] **Step 4: Create `.env` and keep the data dir in git**

```bash
cd /home/redji/reminder-service
cp .env.example .env       # BOT_TOKEN/CHAT_ID stay empty until Task 12
touch data/.gitkeep
```

`.env` is gitignored, so the real token never lands in the repo.

- [ ] **Step 5: Build and start**

```bash
cd /home/redji/reminder-service
docker compose build
docker compose up -d
sleep 8
docker compose ps
docker compose logs --no-color | tail -20
```

Expected: the container is `Up`; the logs show `scheduler started, ticking every 30s` and the `BOT_TOKEN/CHAT_ID not set — Telegram disabled` warning (correct at this stage).

- [ ] **Step 6: Verify the containerised service end to end**

```bash
curl -s http://127.0.0.1:8765/api/healthz
curl -s -o /dev/null -w 'dashboard %{http_code}\n' http://127.0.0.1:8765/
curl -s -X POST http://127.0.0.1:8765/api/reminders \
  -H 'content-type: application/json' \
  -d '{"title":"docker smoke","due_at":"2026-08-12T00:00:00+00:00","retry_interval_min":1,"max_retries":2}'
sleep 35
curl -s http://127.0.0.1:8765/api/reminders
docker compose logs --no-color | grep 'no telegram'
```

Expected: health `ok`, dashboard `200`, the reminder comes back with `retry_count` 1 after the tick, and the log shows the `[no telegram] would send reminder` line.

- [ ] **Step 7: Verify the SQLite file survives a rebuild**

```bash
cd /home/redji/reminder-service
ls -l data/
docker compose down
docker compose up -d
sleep 8
curl -s http://127.0.0.1:8765/api/reminders
```

Expected: `data/reminders.db` exists on the host, and the `docker smoke` reminder is still listed after the restart. If the list comes back empty the volume mount is wrong — fix it before moving on.

- [ ] **Step 8: Clean up the smoke-test row**

```bash
curl -s -X DELETE http://127.0.0.1:8765/api/reminders/1 -o /dev/null -w '%{http_code}\n'
```

Expected: `204`

- [ ] **Step 9: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore data/.gitkeep
git commit -m "feat: docker packaging with persistent sqlite volume"
```

---

### Task 12: Telegram setup, live verification, and README

This is the only task with manual steps — creating the bot has to happen in the Telegram app.

**Files:**
- Create: `README.md`
- Modify: `.env` (not committed)

**Interfaces:**
- Consumes: everything above
- Produces: a working, credentialed deployment

- [ ] **Step 1: Create the bot with @BotFather** *(manual — user does this in Telegram)*

1. Open Telegram, search for **@BotFather**, and start a chat.
2. Send `/newbot`.
3. Give it a display name (anything, e.g. `Redji Reminders`).
4. Give it a username ending in `bot` (e.g. `redji_reminders_bot`) — must be globally unique.
5. BotFather replies with a token shaped like `8123456789:AAH...`. That is `BOT_TOKEN`.
6. **Open a chat with your new bot and send it any message** (e.g. `hi`). Telegram will not let a bot message you first, so this step is what makes your chat reachable.

- [ ] **Step 2: Find your chat id**

With the token in hand:

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates" | python3 -m json.tool
```

Expected: JSON containing `"chat": {"id": 987654321, ...}`. That number is `CHAT_ID`.

If `result` is an empty list, the container is already polling and consuming the updates — run `docker compose stop` first, send the bot another message, then retry.

Alternative: leave the container running, message the bot, and read the chat id straight out of the logs — an unauthorised chat is logged by design:

```bash
docker compose logs --no-color | grep 'unauthorised chat'
```

- [ ] **Step 3: Put the credentials in `.env`**

```bash
cd /home/redji/reminder-service
sed -i 's/^BOT_TOKEN=.*/BOT_TOKEN=8123456789:AAH_replace_me/' .env
sed -i 's/^CHAT_ID=.*/CHAT_ID=987654321/' .env
grep -c . .env
```

Then restart so the new environment is picked up:

```bash
docker compose up -d --force-recreate
sleep 8
docker compose logs --no-color | tail -10
```

Expected: the log now reads `telegram bot polling; authorised chat id 987654321` and the "Telegram disabled" warning is gone.

- [ ] **Step 4: Live test — the inline button ack**

```bash
curl -s -X POST http://127.0.0.1:8765/api/reminders \
  -H 'content-type: application/json' \
  -d "{\"title\":\"button ack test\",\"note\":\"tap Done\",\"due_at\":\"$(date -u -d '-1 minute' +%Y-%m-%dT%H:%M:%SZ)\",\"retry_interval_min\":2,\"max_retries\":3}"
```

Within 30 seconds Telegram should deliver `⏰ button ack test` with a `✅ Done` button. Tap it, then:

```bash
curl -s http://127.0.0.1:8765/api/reminders | python3 -m json.tool
```

Expected: the message edits in place to `✅ Done at HH:MM UTC` with the button gone; the reminder's `status` is `acked`; no further messages arrive.

- [ ] **Step 5: Live test — the text-reply ack**

```bash
curl -s -X POST http://127.0.0.1:8765/api/reminders \
  -H 'content-type: application/json' \
  -d "{\"title\":\"reply ack test\",\"due_at\":\"$(date -u -d '-1 minute' +%Y-%m-%dT%H:%M:%SZ)\",\"retry_interval_min\":2,\"max_retries\":3}"
```

Wait for the message, then reply with any text (e.g. `yep`).

Expected: the bot replies `✅ Marked "reply ack test" done.`, the original message is edited to show Done, and `GET /api/reminders` shows `status: acked`.

- [ ] **Step 6: Live test — retry then expiry**

```bash
curl -s -X POST http://127.0.0.1:8765/api/reminders \
  -H 'content-type: application/json' \
  -d "{\"title\":\"expiry test\",\"due_at\":\"$(date -u -d '-1 minute' +%Y-%m-%dT%H:%M:%SZ)\",\"retry_interval_min\":1,\"max_retries\":2}"
```

Ignore it completely. Expected timeline: one message immediately, a second ~1 minute later, then no more; ~1 minute after the second message the reminder flips to `expired`. Confirm with:

```bash
curl -s "http://127.0.0.1:8765/api/reminders?status=expired" | python3 -m json.tool
```

- [ ] **Step 7: Clean up the test reminders**

```bash
for id in $(curl -s http://127.0.0.1:8765/api/reminders | python3 -c \
  'import json,sys; print(" ".join(str(r["id"]) for r in json.load(sys.stdin)))'); do
  curl -s -X DELETE "http://127.0.0.1:8765/api/reminders/$id" -o /dev/null
done
curl -s http://127.0.0.1:8765/api/reminders
```

Expected: `[]`

- [ ] **Step 8: Write `README.md`**

```markdown
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
```

- [ ] **Step 9: Run the full suite one final time**

```bash
cd /home/redji/reminder-service
.venv/bin/python -m pytest -v
docker compose ps
```

Expected: all tests pass; the container is `Up (healthy)`.

- [ ] **Step 10: Commit**

```bash
git add README.md
git commit -m "docs: setup, behaviour, and configuration reference"
```

---

## Follow-up (explicitly not in this plan)

Deploying to a dedicated Debian LXC on the Proxmox cluster. The repo is
deploy-ready — it needs an LXC with Docker, a clone, an `.env`, and
`docker compose up -d`. That is its own session.
