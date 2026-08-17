from datetime import datetime, time, timedelta
from enum import Enum

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

from app.errors import InvalidRecurrence
from app.models import ReminderStatus
from app.timeutil import from_local_naive, to_local_naive


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


# The explicit, whitelisted RRULE subset. Anything outside it is rejected at
# write time rather than accepted and quietly ignored.
ALLOWED_FREQ = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
ALLOWED_KEYS = ("FREQ", "INTERVAL", "BYDAY")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
RECUR_FROM_VALUES = ("schedule", "completion")

_COMPLETION_DELTA_UNIT = {
    "DAILY": "days",
    "WEEKLY": "weeks",
    "MONTHLY": "months",
    "YEARLY": "years",
}


def parse_recurrence(rule: str) -> dict:
    """Parse and validate the supported RRULE subset.

    Returns {"freq", "interval", "byday"}. Raises InvalidRecurrence naming the
    offending component, so the user learns what to change.
    """
    parts: dict[str, str] = {}
    for chunk in rule.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise InvalidRecurrence(
                f"Not a recurrence rule: {rule!r} (expected e.g. FREQ=WEEKLY;BYDAY=TU)"
            )
        key, _, value = chunk.partition("=")
        parts[key.strip().upper()] = value.strip().upper()

    for key in parts:
        if key not in ALLOWED_KEYS:
            raise InvalidRecurrence(
                f"{key} is not supported; only {', '.join(ALLOWED_KEYS)} are"
            )

    freq = parts.get("FREQ")
    if freq is None:
        raise InvalidRecurrence("FREQ is required, e.g. FREQ=WEEKLY")
    if freq not in ALLOWED_FREQ:
        raise InvalidRecurrence(
            f"FREQ={freq} is not supported; use one of {', '.join(ALLOWED_FREQ)}"
        )

    raw_interval = parts.get("INTERVAL", "1")
    try:
        interval = int(raw_interval)
    except ValueError:
        raise InvalidRecurrence(f"INTERVAL must be a whole number, got {raw_interval!r}") from None
    if interval < 1:
        raise InvalidRecurrence(f"INTERVAL must be at least 1, got {interval}")

    byday = None
    if "BYDAY" in parts:
        if freq != "WEEKLY":
            raise InvalidRecurrence(f"BYDAY is only supported with FREQ=WEEKLY, not {freq}")
        byday = [code.strip() for code in parts["BYDAY"].split(",") if code.strip()]
        unknown = [code for code in byday if code not in WEEKDAY_CODES]
        if not byday or unknown:
            raise InvalidRecurrence(
                f"BYDAY must be a comma-separated list of {', '.join(WEEKDAY_CODES)}"
            )

    return {"freq": freq, "interval": interval, "byday": byday}


def validate_recurrence(rule: str | None, recur_from: str) -> None:
    """Write-time gate for a (rule, anchor) pair. None means one-shot."""
    if recur_from not in RECUR_FROM_VALUES:
        raise InvalidRecurrence(
            f"recur_from must be one of {', '.join(RECUR_FROM_VALUES)}, got {recur_from!r}"
        )
    if rule is None:
        return
    parsed = parse_recurrence(rule)
    if parsed["byday"] and recur_from == "completion":
        raise InvalidRecurrence(
            "BYDAY cannot be combined with recur_from=completion: a weekday set "
            "has no meaning relative to an arbitrary completion time"
        )


def next_occurrence(
    *,
    rule: str,
    recur_from: str,
    previous_due: datetime,
    resolved_at: datetime,
    now: datetime,
    tz: str = "UTC",
) -> datetime:
    """The next due_at for a recurring reminder. Naive UTC in and out.

    Computed in local wall clock so that day boundaries, month lengths, and
    DST transitions mean what the user means by them.
    """
    parsed = parse_recurrence(rule)
    local_now = to_local_naive(now, tz)

    if recur_from == "completion":
        step = relativedelta(**{_COMPLETION_DELTA_UNIT[parsed["freq"]]: parsed["interval"]})
        candidate = to_local_naive(resolved_at, tz) + step
        # Guard against a stale resolution time leaving the series in the past.
        while candidate <= local_now:
            candidate += step
        return from_local_naive(candidate, tz)

    # schedule anchoring: walk the rule forward from the scheduled time until
    # the result is strictly in the future, so a series missed for a week
    # resumes at the next real occurrence rather than firing a backlog.
    local_previous = to_local_naive(previous_due, tz)
    candidate = rrulestr(rule, dtstart=local_previous).after(
        max(local_previous, local_now), inc=False
    )
    if candidate is None:  # pragma: no cover - the subset has no COUNT/UNTIL
        raise InvalidRecurrence(f"{rule!r} has no further occurrences")
    return from_local_naive(candidate, tz)
