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
