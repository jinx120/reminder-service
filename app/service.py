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
