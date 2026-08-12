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
