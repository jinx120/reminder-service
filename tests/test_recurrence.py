from datetime import datetime

import pytest

from app.errors import InvalidRecurrence
from app.logic import next_occurrence, parse_recurrence, validate_recurrence

NOW = datetime(2026, 8, 15, 12, 0, 0)


# --- parsing and validation ---------------------------------------------

def test_minimal_rule_parses():
    assert parse_recurrence("FREQ=DAILY") == \
        {"freq": "DAILY", "interval": 1, "byday": None}


def test_full_rule_parses():
    assert parse_recurrence("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR") == \
        {"freq": "WEEKLY", "interval": 2, "byday": ["MO", "WE", "FR"]}


def test_parsing_is_case_and_space_insensitive():
    assert parse_recurrence(" freq=daily ; interval=3 ")["freq"] == "DAILY"


@pytest.mark.parametrize("rule,offender", [
    ("INTERVAL=2", "FREQ"),
    ("FREQ=HOURLY", "FREQ"),
    ("FREQ=MINUTELY", "FREQ"),
    ("FREQ=DAILY;COUNT=5", "COUNT"),
    ("FREQ=DAILY;UNTIL=20261231T000000Z", "UNTIL"),
    ("FREQ=MONTHLY;BYMONTHDAY=1", "BYMONTHDAY"),
    ("FREQ=DAILY;INTERVAL=0", "INTERVAL"),
    ("FREQ=DAILY;INTERVAL=-1", "INTERVAL"),
    ("FREQ=DAILY;INTERVAL=every", "INTERVAL"),
    ("FREQ=WEEKLY;BYDAY=XX", "BYDAY"),
    ("FREQ=DAILY;BYDAY=MO", "BYDAY"),
    ("not a rule at all", "not a rule at all"),
])
def test_unsupported_components_are_rejected_by_name(rule, offender):
    """Silently accepting an RRULE we do not honour would be worse than
    refusing it — the user would believe a schedule that never runs."""
    with pytest.raises(InvalidRecurrence, match=offender):
        parse_recurrence(rule)


def test_none_is_valid_and_means_one_shot():
    assert validate_recurrence(None, "schedule") is None


def test_byday_with_completion_anchor_is_rejected():
    """A weekday set has no meaning relative to an arbitrary completion
    instant."""
    with pytest.raises(InvalidRecurrence, match="BYDAY"):
        validate_recurrence("FREQ=WEEKLY;BYDAY=MO", "completion")


def test_unknown_recur_from_is_rejected():
    with pytest.raises(InvalidRecurrence, match="recur_from"):
        validate_recurrence("FREQ=DAILY", "whenever")


def test_valid_combinations_pass():
    assert validate_recurrence("FREQ=WEEKLY;BYDAY=TU", "schedule") is None
    assert validate_recurrence("FREQ=DAILY;INTERVAL=3", "completion") is None


# --- schedule anchoring --------------------------------------------------

def call(**overrides) -> datetime:
    kwargs = dict(
        rule="FREQ=DAILY",
        recur_from="schedule",
        previous_due=datetime(2026, 8, 15, 9, 0),
        resolved_at=NOW,
        now=NOW,
        tz="UTC",
    )
    kwargs.update(overrides)
    return next_occurrence(**kwargs)


def test_schedule_daily_advances_one_day_from_the_scheduled_time():
    assert call() == datetime(2026, 8, 16, 9, 0)


def test_schedule_ignores_when_it_was_actually_completed():
    """"Bins out every Tuesday" stays on Tuesdays even when acked late."""
    assert call(resolved_at=datetime(2026, 8, 15, 23, 47)) == datetime(2026, 8, 16, 9, 0)


def test_schedule_weekly_byday_lands_on_the_named_weekday():
    result = call(rule="FREQ=WEEKLY;BYDAY=TU", previous_due=datetime(2026, 8, 11, 9, 0))
    assert result == datetime(2026, 8, 18, 9, 0)
    assert result.strftime("%A") == "Tuesday"


def test_schedule_catches_up_rather_than_firing_a_backlog():
    """A series missed for a week resumes at the NEXT real occurrence."""
    assert call(
        rule="FREQ=WEEKLY;BYDAY=TU",
        previous_due=datetime(2026, 8, 4, 9, 0),
    ) == datetime(2026, 8, 18, 9, 0)


def test_schedule_result_is_always_strictly_in_the_future():
    assert call(rule="FREQ=DAILY", previous_due=datetime(2026, 7, 1, 9, 0)) > NOW


def test_schedule_interval_is_honoured():
    assert call(rule="FREQ=DAILY;INTERVAL=3") == datetime(2026, 8, 18, 9, 0)


def test_schedule_monthly_skips_months_without_that_day():
    assert call(
        rule="FREQ=MONTHLY",
        previous_due=datetime(2026, 1, 31, 9, 0),
        now=datetime(2026, 1, 31, 10, 0),
        resolved_at=datetime(2026, 1, 31, 10, 0),
    ) == datetime(2026, 3, 31, 9, 0)


def test_schedule_yearly():
    assert call(rule="FREQ=YEARLY") == datetime(2027, 8, 15, 9, 0)


def test_schedule_weekday_is_evaluated_in_the_configured_zone():
    """23:30 UTC on Monday is 00:30 Tuesday in Berlin, so a Berlin user's
    "every Tuesday" must anchor on the Berlin weekday."""
    result = next_occurrence(
        rule="FREQ=WEEKLY;BYDAY=TU",
        recur_from="schedule",
        previous_due=datetime(2026, 8, 10, 22, 30),   # 00:30 Tue 11th in Berlin
        resolved_at=datetime(2026, 8, 11, 8, 0),
        now=datetime(2026, 8, 11, 8, 0),
        tz="Europe/Berlin",
    )
    assert result == datetime(2026, 8, 17, 22, 30)    # 00:30 Tue 18th in Berlin


# --- completion anchoring ------------------------------------------------

def test_completion_daily_counts_from_when_it_was_done():
    """"Water the plants every 3 days" means 3 days after you actually did it."""
    assert call(
        rule="FREQ=DAILY;INTERVAL=3",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 15, 14, 23),
    ) == datetime(2026, 8, 18, 14, 23)


def test_completion_weekly():
    assert call(
        rule="FREQ=WEEKLY",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 15, 14, 0),
    ) == datetime(2026, 8, 22, 14, 0)


def test_completion_monthly_clamps_into_a_short_month():
    assert call(
        rule="FREQ=MONTHLY",
        recur_from="completion",
        resolved_at=datetime(2026, 1, 31, 14, 23),
        now=datetime(2026, 1, 31, 14, 23),
    ) == datetime(2026, 2, 28, 14, 23)


def test_completion_yearly_clamps_a_leap_day():
    assert call(
        rule="FREQ=YEARLY",
        recur_from="completion",
        resolved_at=datetime(2028, 2, 29, 9, 0),
        now=datetime(2028, 2, 29, 9, 0),
    ) == datetime(2029, 2, 28, 9, 0)


def test_completion_result_is_pushed_past_now_if_resolution_was_stale():
    assert call(
        rule="FREQ=DAILY",
        recur_from="completion",
        resolved_at=datetime(2026, 8, 1, 9, 0),
        now=NOW,
    ) > NOW
