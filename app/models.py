from datetime import datetime
from enum import Enum

from sqlalchemy import String
from sqlmodel import Field, SQLModel

from app.timeutil import utcnow


class ReminderStatus(str, Enum):
    pending = "pending"
    acked = "acked"
    expired = "expired"


class RecurFrom(str, Enum):
    schedule = "schedule"
    completion = "completion"


class CompletionOutcome(str, Enum):
    completed = "completed"
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
    # NULL means one-shot. A whitelisted RRULE subset — see app/logic.py.
    recurrence: str | None = Field(default=None)
    # Plain str column holding the enum value, same rule as `status`.
    recur_from: str = Field(default=RecurFrom.schedule.value)
    snooze_count: int = Field(default=0)


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    reminder_id: int = Field(foreign_key="reminders.id", index=True)
    sent_at: datetime = Field(default_factory=utcnow)
    acked_at: datetime | None = Field(default=None)
    # Needed so a plain-text-reply ack can edit the message it is acking.
    telegram_message_id: int | None = Field(default=None)


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
    outcome: str = Field(default=CompletionOutcome.completed.value, sa_type=String)
