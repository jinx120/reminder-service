from datetime import datetime, time, timedelta

import pytest

from app.logic import Action, decide, in_quiet_hours

NOW = datetime(2026, 8, 15, 12, 0, 0)
NIGHT = time(22, 0)
MORNING = time(8, 0)


@pytest.mark.parametrize("moment,expected", [
    (time(21, 59), False),
    (time(22, 0), True),
    (time(23, 30), True),
    (time(0, 0), True),
    (time(3, 0), True),
    (time(7, 59), True),
    (time(8, 0), False),      # a reminder deferred overnight fires AT the end
    (time(12, 0), False),
])
def test_window_crossing_midnight(moment, expected):
    assert in_quiet_hours(moment, NIGHT, MORNING) is expected


@pytest.mark.parametrize("moment,expected", [
    (time(8, 59), False),
    (time(9, 0), True),
    (time(16, 59), True),
    (time(17, 0), False),
])
def test_window_within_one_day(moment, expected):
    assert in_quiet_hours(moment, time(9, 0), time(17, 0)) is expected


def test_unset_bounds_disable_the_window():
    assert in_quiet_hours(time(3, 0), None, None) is False
    assert in_quiet_hours(time(3, 0), NIGHT, None) is False
    assert in_quiet_hours(time(3, 0), None, MORNING) is False


def test_zero_width_window_is_disabled_not_always_on():
    assert in_quiet_hours(time(3, 0), time(9, 0), time(9, 0)) is False


def call(**overrides) -> Action:
    kwargs = dict(
        status="pending",
        due_at=NOW - timedelta(minutes=1),
        last_sent_at=None,
        retry_count=0,
        retry_interval_min=15,
        max_retries=4,
        now=NOW,
    )
    kwargs.update(overrides)
    return decide(**kwargs)


def test_due_reminder_is_suppressed_inside_quiet_hours():
    assert call(
        local_now=datetime(2026, 8, 15, 2, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.NOTHING


def test_due_reminder_fires_once_the_window_ends():
    assert call(
        local_now=datetime(2026, 8, 15, 8, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.SEND


def test_quiet_hours_never_expire_a_reminder():
    """No send happens, so no retry is consumed AND no expiry is evaluated —
    a reminder must not be able to quietly die overnight."""
    assert call(
        retry_count=4,
        max_retries=4,
        last_sent_at=NOW - timedelta(hours=5),
        local_now=datetime(2026, 8, 15, 2, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.NOTHING


def test_that_same_reminder_expires_once_the_window_ends():
    assert call(
        retry_count=4,
        max_retries=4,
        last_sent_at=NOW - timedelta(hours=5),
        local_now=datetime(2026, 8, 15, 9, 0, 0),
        quiet_start=NIGHT,
        quiet_end=MORNING,
    ) == Action.EXPIRE


def test_local_now_defaults_to_now_when_omitted():
    assert call(quiet_start=time(11, 0), quiet_end=time(13, 0)) == Action.NOTHING
