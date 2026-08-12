from datetime import datetime, timedelta
from enum import Enum

from app.models import ReminderStatus


class Action(str, Enum):
    NOTHING = "nothing"
    SEND = "send"
    EXPIRE = "expire"


def decide(
    *,
    status: str,
    due_at: datetime,
    last_sent_at: datetime | None,
    retry_count: int,
    retry_interval_min: int,
    max_retries: int,
    now: datetime,
) -> Action:
    """Decide what a single reminder needs right now.

    All datetimes are naive UTC. `retry_count` counts messages already sent,
    so `max_retries` is really a total-send budget.
    """
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
