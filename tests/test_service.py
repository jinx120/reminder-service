from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app import service
from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
    SnoozeLimitReached,
)
from app.models import Completion, CompletionOutcome, Notification, Reminder, ReminderStatus
from app.service import (
    ack_reminder,
    complete_reminder,
    create_reminder,
    delete_reminder,
    due_digest,
    expire_reminder,
    find_reply_ack_target,
    get_reminder,
    latest_notification,
    list_reminders,
    record_send,
    search_reminders,
    snooze_reminder,
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


def test_completing_a_one_shot_reminder_is_terminal(session):
    reminder = make_reminder(session)
    completed = complete_reminder(session, reminder.id, now=NOW)
    assert completed.status == ReminderStatus.acked.value


def test_completing_writes_a_completion_row(session):
    due = NOW - timedelta(hours=1)
    reminder = make_reminder(session, due_at=due)
    complete_reminder(session, reminder.id, now=NOW)

    row = session.exec(select(Completion)).one()
    assert row.reminder_id == reminder.id
    assert row.scheduled_for == due
    assert row.completed_at == NOW
    assert row.outcome == CompletionOutcome.completed.value


def test_completing_stamps_the_latest_notification(session):
    reminder = make_reminder(session)
    session.add(Notification(reminder_id=reminder.id, sent_at=NOW - timedelta(minutes=5)))
    session.commit()

    complete_reminder(session, reminder.id, now=NOW)

    assert session.exec(select(Notification)).one().acked_at == NOW


def test_completing_a_recurring_reminder_rolls_it_forward_in_place(session):
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)

    assert rolled.status == ReminderStatus.pending.value
    assert rolled.due_at == datetime(2026, 8, 16, 9, 0)


def test_roll_forward_resets_the_per_occurrence_counters(session):
    reminder = make_reminder(
        session,
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=DAILY",
        retry_count=3,
        last_sent_at=NOW - timedelta(minutes=20),
        snooze_count=2,
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)

    assert rolled.retry_count == 0
    assert rolled.last_sent_at is None
    assert rolled.snooze_count == 0


def test_roll_forward_records_the_occurrence_that_was_resolved(session):
    """due_at is overwritten in place, so the completions row is the only
    surviving record of the occurrence."""
    due = datetime(2026, 8, 15, 9, 0)
    reminder = make_reminder(session, due_at=due, recurrence="FREQ=DAILY")
    complete_reminder(session, reminder.id, now=NOW)
    assert session.exec(select(Completion)).one().scheduled_for == due


def test_completion_anchored_recurrence_counts_from_now(session):
    reminder = make_reminder(
        session,
        due_at=datetime(2026, 8, 10, 9, 0),
        recurrence="FREQ=DAILY;INTERVAL=3",
        recur_from="completion",
    )
    rolled = complete_reminder(session, reminder.id, now=NOW)
    assert rolled.due_at == NOW + timedelta(days=3)


def test_completing_an_unknown_reminder_raises(session):
    with pytest.raises(ReminderNotFound):
        complete_reminder(session, 999, now=NOW)


def test_completing_an_already_acked_reminder_raises(session):
    reminder = make_reminder(session, status=ReminderStatus.acked.value)
    with pytest.raises(ReminderNotPending, match="acked"):
        complete_reminder(session, reminder.id, now=NOW)


def test_telegram_ack_rolls_a_series_forward_too(session):
    """The bot's ack path must not be a second, divergent implementation."""
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    assert ack_reminder(session, reminder.id, now=NOW) is True
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)


def test_ack_still_returns_false_instead_of_raising(session):
    """Double-taps on the inline button must stay harmless."""
    reminder = make_reminder(session)
    assert ack_reminder(session, reminder.id, now=NOW) is True
    assert ack_reminder(session, reminder.id, now=NOW) is False
    assert ack_reminder(session, 999, now=NOW) is False


def test_snooze_without_a_duration_uses_the_default(session):
    reminder = make_reminder(session)
    snoozed = snooze_reminder(session, reminder.id, default_minutes=15, now=NOW)
    assert snoozed.due_at == NOW + timedelta(minutes=15)


def test_snooze_accepts_a_duration_shorthand(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, duration="2h", now=NOW).due_at == \
        NOW + timedelta(hours=2)


def test_snooze_accepts_an_absolute_phrase(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, duration="in 45 minutes", now=NOW).due_at == \
        NOW + timedelta(minutes=45)


def test_snooze_stays_pending_and_resets_the_send_counters(session):
    reminder = make_reminder(session, retry_count=3, last_sent_at=NOW - timedelta(minutes=5))
    snoozed = snooze_reminder(session, reminder.id, now=NOW)
    assert snoozed.status == ReminderStatus.pending.value
    assert snoozed.retry_count == 0
    assert snoozed.last_sent_at is None


def test_snooze_increments_the_counter(session):
    reminder = make_reminder(session)
    assert snooze_reminder(session, reminder.id, now=NOW).snooze_count == 1
    assert snooze_reminder(session, reminder.id, now=NOW).snooze_count == 2


def test_snooze_is_capped(session):
    """Without a cap a reminder can be deferred forever, which is the same as
    losing it silently."""
    reminder = make_reminder(session, snooze_count=3)
    with pytest.raises(SnoozeLimitReached, match="3"):
        snooze_reminder(session, reminder.id, max_snoozes=3, now=NOW)


def test_snooze_rejects_a_target_in_the_past(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidTime, match="future"):
        snooze_reminder(session, reminder.id, duration="2026-01-01T00:00:00Z", now=NOW)


def test_snooze_rejects_gibberish(session):
    reminder = make_reminder(session)
    with pytest.raises(InvalidTime):
        snooze_reminder(session, reminder.id, duration="in a bit", now=NOW)


def test_snooze_refuses_a_resolved_reminder(session):
    reminder = make_reminder(session, status=ReminderStatus.expired.value)
    with pytest.raises(ReminderNotPending):
        snooze_reminder(session, reminder.id, now=NOW)


def test_expiring_a_one_shot_reminder_is_terminal(session):
    reminder = make_reminder(session)
    expire_reminder(session, reminder, now=NOW)
    session.commit()
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.expired.value
    assert session.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


def test_expiring_a_recurring_reminder_rolls_the_series_forward(session):
    """A single missed occurrence must not silently kill the series — that is
    the failure mode most likely to erode trust in the tool."""
    reminder = make_reminder(
        session, due_at=datetime(2026, 8, 15, 9, 0), recurrence="FREQ=DAILY"
    )
    expire_reminder(session, reminder, now=NOW)
    session.commit()
    session.refresh(reminder)

    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)
    assert reminder.retry_count == 0
    assert session.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


def test_expire_reminder_does_not_commit(session):
    reminder = make_reminder(session)
    expire_reminder(session, reminder, now=NOW)
    session.rollback()
    session.refresh(reminder)
    assert reminder.status == ReminderStatus.pending.value


def test_digest_buckets_by_overdue_today_and_upcoming(session):
    make_reminder(session, title="late", due_at=NOW - timedelta(hours=3))
    make_reminder(session, title="later today", due_at=NOW + timedelta(hours=3))
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))

    digest = due_digest(session, window="week", now=NOW)

    assert [r.title for r in digest["overdue"]] == ["late"]
    assert [r.title for r in digest["due_today"]] == ["later today"]
    assert [r.title for r in digest["upcoming"]] == ["thursday"]


def test_digest_default_window_stops_at_the_end_of_today(session):
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))
    assert due_digest(session, now=NOW)["upcoming"] == []


def test_digest_window_accepts_a_phrase(session):
    make_reminder(session, title="thursday", due_at=NOW + timedelta(days=3))
    assert [r.title for r in due_digest(session, window="in 4 days", now=NOW)["upcoming"]] == \
        ["thursday"]


def test_digest_excludes_resolved_reminders(session):
    make_reminder(session, title="done", due_at=NOW - timedelta(hours=3),
                  status=ReminderStatus.acked.value)
    assert due_digest(session, window="week", now=NOW)["overdue"] == []


def test_digest_day_boundary_follows_the_configured_zone(session):
    """23:00 UTC is already tomorrow in Auckland, so nothing is "today".

    Auckland's local "today" ends at 2026-08-16 11:59:59.999999 UTC (23:00 UTC
    on the 15th is already 2026-08-16 11:00 local). "soon" is due after that
    local day boundary but still within the week window, so it must land in
    upcoming, not due_today.
    """
    now = datetime(2026, 8, 15, 23, 0)
    make_reminder(session, title="soon", due_at=now + timedelta(hours=13))
    digest = due_digest(session, window="week", tz="Pacific/Auckland", now=now)
    assert [r.title for r in digest["upcoming"]] == ["soon"]
    assert digest["due_today"] == []


# --- post-review fixes -----------------------------------------------------


def test_update_reminder_resets_the_send_budget_when_due_at_moves(session):
    """A rescheduled reminder must get a fresh retry budget.

    Without this, a reminder that has already exhausted `max_retries` stays
    `pending` until the tick expires it; edit its due date inside that window
    and `decide()` goes straight to EXPIRE at the new due time, having sent
    nothing. `snooze_reminder` already resets both fields — moving `due_at`
    through `update_reminder` is the same act by a different door.
    """
    reminder = service.create_reminder(
        session, title="pay rent", due_at="2026-08-20T09:00:00Z"
    )
    reminder.retry_count = 4
    reminder.last_sent_at = datetime(2026, 8, 20, 10, 0)
    session.add(reminder)
    session.commit()

    updated = service.update_reminder(
        session, reminder.id, {"due_at": "2026-08-25T09:00:00Z"}
    )

    assert updated.retry_count == 0
    assert updated.last_sent_at is None


def test_update_reminder_leaves_the_send_budget_alone_when_due_at_is_untouched(session):
    reminder = service.create_reminder(
        session, title="pay rent", due_at="2026-08-20T09:00:00Z"
    )
    reminder.retry_count = 2
    reminder.last_sent_at = datetime(2026, 8, 20, 10, 0)
    session.add(reminder)
    session.commit()

    updated = service.update_reminder(session, reminder.id, {"title": "pay the rent"})

    assert updated.retry_count == 2
    assert updated.last_sent_at == datetime(2026, 8, 20, 10, 0)


def test_list_reminders_rejects_a_status_that_is_not_a_real_state(session):
    """An unknown status must be an error, not a confident empty list.

    The completed state is named `acked`, so a caller reaching for
    "completed" or "done" is making the single most likely mistake there is.
    Answering it with `[]` tells them they have nothing.
    """
    with pytest.raises(InvalidField, match="completed"):
        service.list_reminders(session, status="completed")


def test_list_reminders_error_names_the_states_that_do_exist(session):
    with pytest.raises(InvalidField, match="acked"):
        service.list_reminders(session, status="done")


def test_search_reminders_rejects_an_unknown_status(session):
    with pytest.raises(InvalidField, match="acked"):
        service.search_reminders(session, "rent", status="finished")


def test_search_reminders_still_accepts_every_real_status(session):
    for status in ("pending", "acked", "expired"):
        assert service.search_reminders(session, "rent", status=status) == []
