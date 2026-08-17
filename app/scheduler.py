import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.config import Settings
from app.db import Database
from app.logic import Action, decide
from app.models import Reminder, ReminderStatus
from app.service import expire_reminder, record_send
from app.timeutil import to_local_naive, utcnow

logger = logging.getLogger("reminder.scheduler")

Sender = Callable[[Reminder], Awaitable[int | None]]


async def log_sender(reminder: Reminder) -> None:
    """Stand-in sender used when Telegram is not configured."""
    logger.info(
        "[no telegram] would send reminder %s: %s", reminder.id, reminder.title
    )
    return None


async def tick(
    db: Database,
    sender: Sender,
    *,
    settings: Settings,
    now_fn: Callable[[], datetime] = utcnow,
) -> None:
    """One scheduler pass: send what is due, expire what is spent.

    A failing send is logged and skipped without touching that reminder's
    counters, so the next tick retries it rather than burning an attempt.
    A reminder whose recurrence rule cannot be computed is likewise left
    untouched. One bad reminder never blocks the others.
    """
    now = now_fn()
    local_now = to_local_naive(now, settings.timezone)

    with db.session() as session:
        pending = session.exec(
            select(Reminder).where(Reminder.status == ReminderStatus.pending.value)
        ).all()

        for reminder in pending:
            action = decide(
                status=reminder.status,
                due_at=reminder.due_at,
                last_sent_at=reminder.last_sent_at,
                retry_count=reminder.retry_count,
                retry_interval_min=reminder.retry_interval_min,
                max_retries=reminder.max_retries,
                now=now,
                local_now=local_now,
                quiet_start=settings.quiet_hours_start,
                quiet_end=settings.quiet_hours_end,
            )

            if action is Action.SEND:
                try:
                    message_id = await sender(reminder)
                except Exception:
                    logger.exception(
                        "failed to send reminder %s (%s); will retry next tick",
                        reminder.id,
                        reminder.title,
                    )
                    continue
                record_send(session, reminder, now=now, message_id=message_id)
                logger.info(
                    "sent reminder %s (%s), attempt %s/%s",
                    reminder.id,
                    reminder.title,
                    reminder.retry_count,
                    reminder.max_retries,
                )

            elif action is Action.EXPIRE:
                try:
                    expire_reminder(session, reminder, now=now, tz=settings.timezone)
                except Exception:
                    logger.exception(
                        "could not resolve reminder %s (%s); leaving it untouched",
                        reminder.id,
                        reminder.title,
                    )
                    session.rollback()
                    continue
                logger.info(
                    "resolved reminder %s (%s) after %s attempts; now %s, due %s",
                    reminder.id,
                    reminder.title,
                    reminder.retry_count,
                    reminder.status,
                    reminder.due_at,
                )

        session.commit()


def build_scheduler(db: Database, sender: Sender, settings: Settings) -> AsyncIOScheduler:
    """An AsyncIOScheduler that runs `tick` on the app's own event loop.

    max_instances=1 plus coalesce=True mean a slow tick can never overlap
    itself or replay a backlog of missed runs — either would double-send.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger="interval",
        seconds=settings.tick_seconds,
        args=[db, sender],
        kwargs={"settings": settings},
        id="reminder-tick",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler
