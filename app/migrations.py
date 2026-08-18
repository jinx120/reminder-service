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
