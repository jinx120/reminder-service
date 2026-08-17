from dataclasses import replace
from datetime import datetime, time, timedelta

from sqlmodel import select

from app.models import Completion, CompletionOutcome, Notification, Reminder, ReminderStatus
from app.scheduler import build_scheduler, tick

NOW = datetime(2026, 8, 12, 12, 0, 0)


class FakeSender:
    """Records what it was asked to send; hands back fake Telegram message ids."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.sent: list[int] = []
        self.fail_on = fail_on or set()
        self._next_message_id = 1000

    async def __call__(self, reminder: Reminder) -> int:
        if reminder.id in self.fail_on:
            raise RuntimeError("telegram is down")
        self.sent.append(reminder.id)
        self._next_message_id += 1
        return self._next_message_id


def add(db, **overrides) -> int:
    fields = dict(title="t", due_at=NOW - timedelta(hours=1))
    fields.update(overrides)
    with db.session() as s:
        reminder = Reminder(**fields)
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        return reminder.id


def load(db, reminder_id: int) -> Reminder:
    with db.session() as s:
        return s.get(Reminder, reminder_id)


async def test_sends_a_due_reminder_and_records_it(db, settings):
    reminder_id = add(db)
    sender = FakeSender()

    await tick(db, sender, settings=settings, now_fn=lambda: NOW)

    assert sender.sent == [reminder_id]
    reminder = load(db, reminder_id)
    assert reminder.retry_count == 1
    assert reminder.last_sent_at == NOW
    assert reminder.status == ReminderStatus.pending.value

    with db.session() as s:
        notification = s.exec(select(Notification)).one()
    assert notification.reminder_id == reminder_id
    assert notification.sent_at == NOW
    assert notification.telegram_message_id == 1001


async def test_does_not_send_a_reminder_that_is_not_due(db, settings):
    add(db, due_at=NOW + timedelta(hours=1))
    sender = FakeSender()
    await tick(db, sender, settings=settings, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_does_not_resend_within_the_retry_interval(db, settings):
    add(db, last_sent_at=NOW - timedelta(minutes=5), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, settings=settings, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_resends_after_the_retry_interval(db, settings):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, settings=settings, now_fn=lambda: NOW)
    assert sender.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 2


async def test_expires_after_the_send_budget_is_spent(db, settings):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15),
                      retry_count=4, max_retries=4)
    sender = FakeSender()

    await tick(db, sender, settings=settings, now_fn=lambda: NOW)

    assert sender.sent == []
    assert load(db, reminder_id).status == ReminderStatus.expired.value


async def test_ignores_acked_reminders(db, settings):
    add(db, status=ReminderStatus.acked.value)
    sender = FakeSender()
    await tick(db, sender, settings=settings, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_a_send_failure_does_not_advance_counters_or_stop_the_tick(db, settings):
    broken_id = add(db, title="broken")
    healthy_id = add(db, title="healthy")
    sender = FakeSender(fail_on={broken_id})

    await tick(db, sender, settings=settings, now_fn=lambda: NOW)

    assert sender.sent == [healthy_id]
    broken = load(db, broken_id)
    assert broken.retry_count == 0
    assert broken.last_sent_at is None
    assert broken.status == ReminderStatus.pending.value
    assert load(db, healthy_id).retry_count == 1


async def test_a_failed_send_is_retried_on_the_next_tick(db, settings):
    reminder_id = add(db)
    await tick(db, FakeSender(fail_on={reminder_id}), settings=settings, now_fn=lambda: NOW)

    recovered = FakeSender()
    await tick(db, recovered, settings=settings, now_fn=lambda: NOW)

    assert recovered.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 1


async def test_tick_with_no_reminders_is_a_no_op(db, settings):
    sender = FakeSender()
    await tick(db, sender, settings=settings, now_fn=lambda: NOW)
    assert sender.sent == []


def test_build_scheduler_registers_a_single_non_overlapping_job(db, settings):
    scheduler = build_scheduler(db, FakeSender(), replace(settings, tick_seconds=30))
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "reminder-tick"
    assert jobs[0].max_instances == 1
    assert jobs[0].trigger.interval.total_seconds() == 30


# `FakeSender`, `add`, and `load` already exist at the top of this file — reuse
# them rather than adding a parallel set of helpers.


async def test_quiet_hours_suppress_a_due_send(db, settings):
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    assert sender.sent == []


async def test_the_same_reminder_sends_once_the_window_ends(db, settings):
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    reminder_id = add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 8, 0))

    assert sender.sent == [reminder_id]


async def test_quiet_hours_never_burn_a_retry_or_expire(db, settings):
    """No send happens, so neither the retry budget nor expiry advances — a
    reminder must not be able to quietly die overnight."""
    quiet = replace(settings, quiet_hours_start=time(22, 0), quiet_hours_end=time(8, 0))
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 1, 0),
        retry_count=4,
        max_retries=4,
        last_sent_at=datetime(2026, 8, 14, 20, 0),
    )

    await tick(db, FakeSender(), settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    reminder = load(db, reminder_id)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.retry_count == 4


async def test_quiet_hours_are_evaluated_in_the_configured_zone(db, settings):
    """02:00 UTC is 04:00 in Berlin — inside a 22:00-08:00 Berlin window."""
    quiet = replace(
        settings,
        timezone="Europe/Berlin",
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(8, 0),
    )
    add(db, due_at=datetime(2026, 8, 15, 1, 0))
    sender = FakeSender()

    await tick(db, sender, settings=quiet, now_fn=lambda: datetime(2026, 8, 15, 2, 0))

    assert sender.sent == []


async def test_expiring_a_recurring_reminder_rolls_it_forward(db, settings):
    now = datetime(2026, 8, 15, 12, 0)
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=DAILY",
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )

    await tick(db, FakeSender(), settings=settings, now_fn=lambda: now)

    reminder = load(db, reminder_id)
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.due_at == datetime(2026, 8, 16, 9, 0)
    assert reminder.retry_count == 0
    with db.session() as s:
        assert s.exec(select(Completion)).one().outcome == CompletionOutcome.expired.value


async def test_expiring_a_one_shot_reminder_is_still_terminal(db, settings):
    now = datetime(2026, 8, 15, 12, 0)
    reminder_id = add(
        db,
        due_at=datetime(2026, 8, 15, 9, 0),
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )

    await tick(db, FakeSender(), settings=settings, now_fn=lambda: now)

    assert load(db, reminder_id).status == ReminderStatus.expired.value


async def test_a_broken_recurrence_rule_does_not_abort_the_tick(db, settings):
    """One reminder with an uncomputable rule must never stop the others from
    being processed."""
    now = datetime(2026, 8, 15, 12, 0)
    broken_id = add(
        db,
        title="broken",
        due_at=datetime(2026, 8, 15, 9, 0),
        recurrence="FREQ=NONSENSE",
        retry_count=4,
        max_retries=4,
        last_sent_at=now - timedelta(hours=2),
    )
    fine_id = add(db, title="fine", due_at=datetime(2026, 8, 15, 11, 0))
    sender = FakeSender()

    await tick(db, sender, settings=settings, now_fn=lambda: now)

    assert sender.sent == [fine_id]
    assert load(db, broken_id).status == ReminderStatus.pending.value
