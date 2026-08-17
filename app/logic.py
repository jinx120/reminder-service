from datetime import datetime, time, timedelta
from enum import Enum

from app.models import ReminderStatus


class Action(str, Enum):
    NOTHING = "nothing"
    SEND = "send"
    EXPIRE = "expire"


def in_quiet_hours(moment: time, start: time | None, end: time | None) -> bool:
    """Is this local time-of-day inside the configured quiet window?

    A window with start > end crosses midnight (22:00-08:00 is the obvious
    case). The end bound is exclusive, so a reminder deferred overnight fires
    at exactly the end of the window rather than a tick later.
    """
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def decide(
    *,
    status: str,
    due_at: datetime,
    last_sent_at: datetime | None,
    retry_count: int,
    retry_interval_min: int,
    max_retries: int,
    now: datetime,
    local_now: datetime | None = None,
    quiet_start: time | None = None,
    quiet_end: time | None = None,
) -> Action:
    """Decide what a single reminder needs right now.

    All datetimes are naive UTC. `retry_count` counts messages already sent,
    so `max_retries` is really a total-send budget.

    `local_now` is the same instant expressed in the configured zone; quiet
    hours are a wall-clock concept, and the caller owns the conversion so
    this module stays pure.
    """
    # Short-circuit before anything else: inside quiet hours no send happens,
    # so no retry is consumed and no expiry is evaluated. A reminder cannot
    # quietly die overnight.
    if in_quiet_hours((local_now or now).time(), quiet_start, quiet_end):
        return Action.NOTHING

    if status != ReminderStatus.pending.value:
        return Action.NOTHING

    if due_at > now:
        return Action.NOTHING

    if last_sent_at is None:
        return Action.SEND

    if now - last_sent_at < timedelta(minutes=retry_interval_min):
        # Too soon to nag again — and too soon to give up, since the user
        # still has the rest of this interval to acknowledge the last message.
        return Action.NOTHING

    if retry_count < max_retries:
        return Action.SEND

    return Action.EXPIRE
