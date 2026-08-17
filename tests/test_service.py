from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
)
from app.models import Notification, Reminder, ReminderStatus
from app.service import (
    ack_reminder,
    create_reminder,
    delete_reminder,
    find_reply_ack_target,
    get_reminder,
    latest_notification,
    list_reminders,
    record_send,
    search_reminders,
    update_reminder,
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


def test_create_stores_the_reminder(session):
    reminder = create_reminder(
        session, title="bins", due_at=NOW, note="green one", now=NOW
    )
    assert reminder.id is not None
    assert reminder.title == "bins"
    assert reminder.note == "green one"
    assert reminder.due_at == NOW
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.recurrence is None
    assert reminder.recur_from == "schedule"


def test_create_resolves_a_natural_language_due_at(session):
    reminder = create_reminder(session, title="t", due_at="in 2 hours", now=NOW)
    assert reminder.due_at == NOW + timedelta(hours=2)


def test_create_rejects_an_unparseable_due_at(session):
    with pytest.raises(InvalidTime):
        create_reminder(session, title="t", due_at="whenever-ish", now=NOW)


def test_create_validates_the_recurrence_rule(session):
    with pytest.raises(InvalidRecurrence, match="FREQ"):
        create_reminder(session, title="t", due_at=NOW, recurrence="FREQ=HOURLY", now=NOW)


def test_create_rejects_byday_with_completion_anchor(session):
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        create_reminder(
            session, title="t", due_at=NOW,
            recurrence="FREQ=WEEKLY;BYDAY=MO", recur_from="completion", now=NOW,
        )


def test_list_is_ordered_by_due_then_id(session):
    make_reminder(session, title="late", due_at=NOW + timedelta(hours=2))
    make_reminder(session, title="soon", due_at=NOW + timedelta(minutes=5))
    assert [r.title for r in list_reminders(session)] == ["soon", "late"]


def test_list_filters_by_status_and_honours_limit(session):
    make_reminder(session, title="a")
    make_reminder(session, title="b")
    make_reminder(session, title="c", status=ReminderStatus.acked.value)
    assert len(list_reminders(session, status="pending")) == 2
    assert len(list_reminders(session, limit=1)) == 1


def test_get_raises_for_an_unknown_id(session):
    with pytest.raises(ReminderNotFound, match="404|not found|999"):
        get_reminder(session, 999)


def test_update_applies_only_the_given_fields(session):
    reminder = make_reminder(session, title="old", note="keep")
    updated = update_reminder(session, reminder.id, {"title": "new"}, now=NOW)
    assert updated.title == "new"
    assert updated.note == "keep"


def test_update_resolves_a_natural_language_due_at(session):
    reminder = make_reminder(session)
    updated = update_reminder(session, reminder.id, {"due_at": "in 30 minutes"}, now=NOW)
    assert updated.due_at == NOW + timedelta(minutes=30)


def test_update_refuses_a_resolved_reminder(session):
    reminder = make_reminder(session, status=ReminderStatus.acked.value)
    with pytest.raises(ReminderNotPending, match="acked"):
        update_reminder(session, reminder.id, {"title": "x"}, now=NOW)


def test_update_validates_recurrence_against_the_stored_anchor(session):
    """Changing only the rule must still be checked against the anchor
    already on the row, not against the default."""
    reminder = make_reminder(session, recur_from="completion")
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        update_reminder(session, reminder.id, {"recurrence": "FREQ=WEEKLY;BYDAY=MO"}, now=NOW)


def test_update_can_clear_a_recurrence(session):
    reminder = make_reminder(session, recurrence="FREQ=DAILY")
    updated = update_reminder(session, reminder.id, {"recurrence": None}, now=NOW)
    assert updated.recurrence is None


def test_update_rejects_an_unknown_field(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidField, match="status"):
        update_reminder(session, reminder.id, {"status": "acked"}, now=NOW)


def test_update_refuses_to_null_a_required_field(session):
    """An explicit null on a NOT NULL column must be a 4xx, not a 500."""
    reminder = make_reminder(session)
    with pytest.raises(InvalidField, match="recur_from"):
        update_reminder(session, reminder.id, {"recur_from": None}, now=NOW)


def test_update_can_still_clear_a_note(session):
    reminder = make_reminder(session, note="old")
    assert update_reminder(session, reminder.id, {"note": None}, now=NOW).note is None


def test_delete_removes_the_reminder_and_its_notifications(session):
    reminder = make_reminder(session)
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW))
    session.commit()

    delete_reminder(session, reminder.id)

    assert session.get(Reminder, reminder.id) is None
    assert session.exec(select(Notification)).all() == []


def test_delete_raises_for_an_unknown_id(session):
    with pytest.raises(ReminderNotFound):
        delete_reminder(session, 999)


def test_search_matches_title_and_note_case_insensitively(session):
    make_reminder(session, title="Take the Bins out")
    make_reminder(session, title="call mum", note="about the BINS")
    make_reminder(session, title="unrelated")
    assert {r.title for r in search_reminders(session, "bins")} == \
        {"Take the Bins out", "call mum"}


def test_search_can_be_narrowed_by_status(session):
    make_reminder(session, title="bins now")
    make_reminder(session, title="bins done", status=ReminderStatus.acked.value)
    assert [r.title for r in search_reminders(session, "bins", status="pending")] == \
        ["bins now"]
