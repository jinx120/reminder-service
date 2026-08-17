import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser

from app.errors import InvalidTime

_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
# dateparser returns None for "next monday" but parses bare "monday" fine.
_NEXT_WEEKDAY = re.compile(rf"^next\s+({_WEEKDAYS})\b", re.IGNORECASE)

_DURATION_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}
_DURATION_TOKEN = re.compile(r"(\d+)\s*([a-z]*)", re.IGNORECASE)
_DURATION_SHAPE = re.compile(r"^(\d+\s*[a-z]*\s*)+$", re.IGNORECASE)

_DATEPARSER_BASE = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "future",
    "TO_TIMEZONE": "UTC",
}


def utcnow() -> datetime:
    """Current UTC time as a naive datetime — the storage form used everywhere."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    """Normalise any datetime to naive UTC.

    A naive input is assumed to already be UTC and passes through untouched.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc_iso(dt: datetime | None) -> str | None:
    """Render a stored naive-UTC datetime as an explicit UTC ISO-8601 string.

    The trailing +00:00 is what lets the browser convert to local time correctly.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def to_local_naive(dt: datetime, tz: str) -> datetime:
    """Stored naive-UTC -> naive wall clock in `tz`."""
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def from_local_naive(dt: datetime, tz: str) -> datetime:
    """Naive wall clock in `tz` -> stored naive UTC."""
    return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)


def as_local_iso(dt: datetime | None, tz: str) -> str | None:
    """Render a stored naive-UTC datetime as an ISO string in `tz`, with offset."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).isoformat()


def parse_when(text: str, *, tz: str, now: datetime | None = None) -> datetime:
    """Resolve ISO-8601 or natural language to naive UTC.

    ISO is tried first so explicit offsets and `Z` are honoured exactly; a
    naive ISO string is read as wall clock in `tz`, not as UTC. Natural
    language falls through to dateparser anchored on `now`.

    Raises InvalidTime rather than guessing — see spec §9.
    """
    if not text or not text.strip():
        raise InvalidTime("No date/time given")
    text = text.strip()
    now = now or utcnow()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        return to_utc_naive(parsed) if parsed.tzinfo else from_local_naive(parsed, tz)

    normalised = _NEXT_WEEKDAY.sub(r"\1", text)
    parsed = dateparser.parse(
        normalised,
        settings={**_DATEPARSER_BASE, "TIMEZONE": tz, "RELATIVE_BASE": to_local_naive(now, tz)},
    )
    if parsed is None:
        raise InvalidTime(f"Could not understand the date/time {text!r}")
    return to_utc_naive(parsed)


def parse_duration_minutes(text: str) -> int | None:
    """Parse a duration shorthand into whole minutes.

    Returns None when the text is not a duration at all, so callers can fall
    back to parse_when for absolute phrasings like "tomorrow at 9am".
    """
    if not text or not text.strip():
        return None
    candidate = text.strip().lower()
    if not _DURATION_SHAPE.match(candidate):
        return None

    total = 0
    for amount, unit in _DURATION_TOKEN.findall(candidate):
        if not unit:
            total += int(amount)  # bare number means minutes
            continue
        if unit not in _DURATION_UNITS:
            return None
        total += int(amount) * _DURATION_UNITS[unit]
    return total or None
