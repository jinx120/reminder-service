from datetime import datetime, timedelta, timezone

from app.timeutil import as_utc_iso, to_utc_naive, utcnow


def test_utcnow_is_naive_and_close_to_now():
    now = utcnow()
    assert now.tzinfo is None
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(reference - now) < timedelta(seconds=5)


def test_to_utc_naive_converts_aware_datetime():
    aware = datetime(2026, 8, 12, 15, 30, tzinfo=timezone(timedelta(hours=5)))
    assert to_utc_naive(aware) == datetime(2026, 8, 12, 10, 30)
    assert to_utc_naive(aware).tzinfo is None


def test_to_utc_naive_passes_naive_through_unchanged():
    naive = datetime(2026, 8, 12, 15, 30)
    assert to_utc_naive(naive) == naive


def test_to_utc_naive_handles_negative_offset():
    aware = datetime(2026, 8, 12, 1, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert to_utc_naive(aware) == datetime(2026, 8, 12, 7, 0)


def test_as_utc_iso_marks_the_value_as_utc():
    assert as_utc_iso(datetime(2026, 8, 12, 10, 30)) == "2026-08-12T10:30:00+00:00"


def test_as_utc_iso_passes_none_through():
    assert as_utc_iso(None) is None
