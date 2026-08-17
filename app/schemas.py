from pydantic import BaseModel, Field

from app.models import Completion, Notification, Reminder
from app.timeutil import as_utc_iso


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


class NotificationRead(BaseModel):
    id: int
    sent_at: str
    acked_at: str | None


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
    recurrence: str | None
    recur_from: str
    snooze_count: int


class ReminderDetail(ReminderRead):
    notifications: list[NotificationRead]
    completions: list[CompletionRead]


def to_notification_read(notification: Notification) -> NotificationRead:
    return NotificationRead(
        id=notification.id,
        sent_at=as_utc_iso(notification.sent_at),
        acked_at=as_utc_iso(notification.acked_at),
    )


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
