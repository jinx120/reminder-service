from datetime import datetime

from sqlmodel import select

from app.models import (
    Completion,
    CompletionOutcome,
    RecurFrom,
    Reminder,
)

NOW = datetime(2026, 8, 15, 12, 0, 0)


def test_new_reminder_columns_default_to_one_shot_behaviour(session):
    reminder = Reminder(title="t", due_at=NOW)
    session.add(reminder)
    session.commit()
    session.refresh(reminder)

    assert reminder.recurrence is None
    assert reminder.recur_from == RecurFrom.schedule.value
    assert reminder.snooze_count == 0


def test_reminder_stores_a_recurrence_rule(session):
    reminder = Reminder(
        title="bins",
        due_at=NOW,
        recurrence="FREQ=WEEKLY;BYDAY=TU",
        recur_from=RecurFrom.schedule.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    assert reminder.recurrence == "FREQ=WEEKLY;BYDAY=TU"


def test_completion_row_records_the_occurrence_it_resolved(session):
    reminder = Reminder(title="t", due_at=NOW)
    session.add(reminder)
    session.commit()
    session.refresh(reminder)

    session.add(
        Completion(
            reminder_id=reminder.id,
            scheduled_for=NOW,
            completed_at=NOW,
            outcome=CompletionOutcome.completed.value,
        )
    )
    session.commit()

    row = session.exec(select(Completion)).one()
    assert row.reminder_id == reminder.id
    assert row.scheduled_for == NOW
    assert row.outcome == "completed"


def test_outcome_is_a_plain_string_column_not_an_enum_type(session):
    """The same trap as `status`: a SQLAlchemy Enum validates by member NAME
    and breaks on value-based writes."""
    column = Completion.__table__.columns["outcome"]
    assert column.type.python_type is str
