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
