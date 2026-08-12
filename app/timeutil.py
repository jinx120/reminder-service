from datetime import datetime, timezone


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
