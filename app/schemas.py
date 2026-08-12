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
