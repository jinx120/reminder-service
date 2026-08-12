from datetime import datetime, timedelta

from sqlmodel import select

from app.models import Notification, Reminder, ReminderStatus
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


async def test_sends_a_due_reminder_and_records_it(db):
    reminder_id = add(db)
    sender = FakeSender()

    await tick(db, sender, now_fn=lambda: NOW)

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


async def test_does_not_send_a_reminder_that_is_not_due(db):
    add(db, due_at=NOW + timedelta(hours=1))
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_does_not_resend_within_the_retry_interval(db):
    add(db, last_sent_at=NOW - timedelta(minutes=5), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_resends_after_the_retry_interval(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15), retry_count=1)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 2


async def test_expires_after_the_send_budget_is_spent(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=15),
                      retry_count=4, max_retries=4)
    sender = FakeSender()

    await tick(db, sender, now_fn=lambda: NOW)

    assert sender.sent == []
    assert load(db, reminder_id).status == ReminderStatus.expired.value


async def test_ignores_acked_reminders(db):
    add(db, status=ReminderStatus.acked.value)
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


async def test_a_send_failure_does_not_advance_counters_or_stop_the_tick(db):
    broken_id = add(db, title="broken")
    healthy_id = add(db, title="healthy")
    sender = FakeSender(fail_on={broken_id})

    await tick(db, sender, now_fn=lambda: NOW)

    assert sender.sent == [healthy_id]
    broken = load(db, broken_id)
    assert broken.retry_count == 0
    assert broken.last_sent_at is None
    assert broken.status == ReminderStatus.pending.value
    assert load(db, healthy_id).retry_count == 1


async def test_a_failed_send_is_retried_on_the_next_tick(db):
    reminder_id = add(db)
    await tick(db, FakeSender(fail_on={reminder_id}), now_fn=lambda: NOW)

    recovered = FakeSender()
    await tick(db, recovered, now_fn=lambda: NOW)

    assert recovered.sent == [reminder_id]
    assert load(db, reminder_id).retry_count == 1


async def test_tick_with_no_reminders_is_a_no_op(db):
    sender = FakeSender()
    await tick(db, sender, now_fn=lambda: NOW)
    assert sender.sent == []


def test_build_scheduler_registers_a_single_non_overlapping_job(db):
    scheduler = build_scheduler(db, FakeSender(), tick_seconds=30)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "reminder-tick"
    assert jobs[0].max_instances == 1
    assert jobs[0].trigger.interval.total_seconds() == 30
