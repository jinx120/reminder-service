from datetime import datetime

import pytest

from app.errors import InvalidTime
from app.timeutil import (
    as_local_iso,
    from_local_naive,
    parse_duration_minutes,
    parse_when,
    to_local_naive,
)

# 2026-08-15 12:00 UTC == 13:00 Europe/London (BST)
NOW = datetime(2026, 8, 15, 12, 0, 0)
LONDON = "Europe/London"


def test_to_local_naive_shifts_into_the_zone():
    assert to_local_naive(NOW, LONDON) == datetime(2026, 8, 15, 13, 0, 0)


def test_from_local_naive_is_the_inverse():
    assert from_local_naive(datetime(2026, 8, 15, 13, 0, 0), LONDON) == NOW


def test_utc_round_trip_is_identity():
    assert from_local_naive(to_local_naive(NOW, "UTC"), "UTC") == NOW


def test_as_local_iso_renders_with_the_zone_offset():
    assert as_local_iso(NOW, LONDON) == "2026-08-15T13:00:00+01:00"


def test_as_local_iso_passes_none_through():
    assert as_local_iso(None, LONDON) is None


def test_iso_with_explicit_offset_is_honoured():
    assert parse_when("2026-09-01T10:00:00+02:00", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 8, 0, 0)


def test_iso_with_z_suffix_is_honoured():
    assert parse_when("2026-09-01T10:00:00Z", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 10, 0, 0)


def test_naive_iso_is_read_in_the_configured_zone():
    """A bare wall-clock string means local wall clock, not UTC — this is the
    single most damaging place to guess wrong."""
    assert parse_when("2026-09-01T10:00:00", tz=LONDON, now=NOW) == \
        datetime(2026, 9, 1, 9, 0, 0)


def test_relative_hours():
    assert parse_when("in 2 hours", tz="UTC", now=NOW) == datetime(2026, 8, 15, 14, 0, 0)


def test_relative_minutes():
    assert parse_when("in 30 minutes", tz="UTC", now=NOW) == datetime(2026, 8, 15, 12, 30, 0)


def test_tomorrow_at_a_time_resolves_in_the_configured_zone():
    # 09:00 London on the 16th is 08:00 UTC.
    assert parse_when("tomorrow at 9am", tz=LONDON, now=NOW) == \
        datetime(2026, 8, 16, 8, 0, 0)


def test_next_weekday_is_normalised_before_parsing():
    """dateparser returns None for "next monday" even though "monday" parses.
    Without normalisation this common phrasing would be a hard error."""
    result = parse_when("next monday", tz="UTC", now=NOW)
    assert result.weekday() == 0
    assert result > NOW


@pytest.mark.parametrize("text", ["", "   ", "asdkjfh", "sometime soonish"])
def test_unparseable_input_raises_rather_than_guessing(text):
    with pytest.raises(InvalidTime):
        parse_when(text, tz="UTC", now=NOW)


def test_the_error_message_quotes_the_offending_input():
    with pytest.raises(InvalidTime, match="asdkjfh"):
        parse_when("asdkjfh", tz="UTC", now=NOW)


@pytest.mark.parametrize("text,expected", [
    ("30m", 30), ("30 min", 30), ("30 minutes", 30),
    ("2h", 120), ("2 hours", 120), ("1h30m", 90),
    ("1d", 1440), ("1 day", 1440), ("45", 45),
])
def test_duration_shorthand(text, expected):
    assert parse_duration_minutes(text) == expected


@pytest.mark.parametrize("text", ["tomorrow at 9am", "", "banana"])
def test_non_durations_return_none_for_the_caller_to_fall_back(text):
    assert parse_duration_minutes(text) is None
