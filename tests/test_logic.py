from datetime import datetime, timedelta

import pytest

from app.logic import Action, decide

NOW = datetime(2026, 8, 12, 12, 0, 0)


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


@pytest.mark.parametrize("status", ["acked", "expired"])
def test_non_pending_reminders_are_left_alone(status):
    assert call(status=status) == Action.NOTHING


def test_reminder_due_in_the_future_is_not_sent():
    assert call(due_at=NOW + timedelta(minutes=1)) == Action.NOTHING


def test_first_send_happens_once_due():
    assert call(due_at=NOW - timedelta(seconds=1), last_sent_at=None) == Action.SEND


def test_reminder_due_exactly_now_is_sent():
    assert call(due_at=NOW, last_sent_at=None) == Action.SEND


def test_no_resend_before_the_retry_interval_elapses():
    assert call(last_sent_at=NOW - timedelta(minutes=5), retry_count=1) == Action.NOTHING


def test_resend_once_the_interval_has_exactly_elapsed():
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=1) == Action.SEND


def test_resend_while_under_the_send_budget():
    assert call(last_sent_at=NOW - timedelta(minutes=20), retry_count=3,
                max_retries=4) == Action.SEND


def test_expires_once_the_budget_is_spent_and_the_interval_elapsed():
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=4,
                max_retries=4) == Action.EXPIRE


def test_does_not_expire_immediately_after_the_final_send():
    """The spec's pseudocode would expire here, killing the last chance to ack."""
    assert call(last_sent_at=NOW - timedelta(seconds=30), retry_count=4,
                max_retries=4) == Action.NOTHING


def test_custom_interval_is_respected():
    assert call(last_sent_at=NOW - timedelta(minutes=3), retry_count=1,
                retry_interval_min=2) == Action.SEND
    assert call(last_sent_at=NOW - timedelta(minutes=1), retry_count=1,
                retry_interval_min=2) == Action.NOTHING


def test_max_retries_of_one_sends_once_then_expires():
    assert call(last_sent_at=None, retry_count=0, max_retries=1) == Action.SEND
    assert call(last_sent_at=NOW - timedelta(minutes=15), retry_count=1,
                max_retries=1) == Action.EXPIRE
