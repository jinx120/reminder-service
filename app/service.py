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
from app.logic import next_occurrence, validate_recurrence
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
    "complete_reminder",
]

MUTABLE_FIELDS = frozenset({
    "title", "note", "due_at", "recurrence", "recur_from",
    "retry_interval_min", "max_retries",
})
# `note` and `recurrence` are the only two a client may legitimately clear.
# Without this guard an explicit JSON null on any other field would reach the
# database as a NOT NULL violation, i.e. a 500 where a 4xx belongs.
CLEARABLE_FIELDS = frozenset({"note", "recurrence"})


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


def latest_notification(session: Session, reminder_id: int) -> Notification | None:
    """The most recent notification sent for a reminder, if any."""
    return session.exec(
        select(Notification)
        .where(Notification.reminder_id == reminder_id)
        .order_by(Notification.sent_at.desc(), Notification.id.desc())
    ).first()


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
