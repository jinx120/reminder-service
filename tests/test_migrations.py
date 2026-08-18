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
