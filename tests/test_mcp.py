import json
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp_server import build_mcp
from app.models import Reminder, ReminderStatus


@pytest.fixture
def mcp(db, settings):
    return build_mcp(db, settings)


async def call(mcp, name, **args) -> dict:
    """MCP 2.0 returns a plain dict as JSON text with structured_content=None."""
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


def seed(db, **overrides) -> int:
    fields = dict(title="t", due_at=datetime(2026, 8, 15, 9, 0))
    fields.update(overrides)
    with db.session() as session:
        reminder = Reminder(**fields)
        session.add(reminder)
        session.commit()
        session.refresh(reminder)
        return reminder.id


async def test_every_spec_tool_is_registered(mcp):
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "create_reminder", "list_reminders", "get_reminder", "update_reminder",
        "complete_reminder", "snooze_reminder", "delete_reminder",
        "search_reminders", "whats_due",
    }


async def test_tool_descriptions_name_the_configured_timezone(mcp):
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "UTC" in tools["create_reminder"].description


async def test_every_result_carries_the_timezone_and_a_fresh_server_time(mcp):
    body = await call(mcp, "list_reminders")
    assert body["timezone"] == "UTC"
    assert body["server_time"].endswith("+00:00")


# --- create ---------------------------------------------------------------

async def test_create_echoes_the_resolved_absolute_time(mcp):
    """A misparse must be visible immediately, not days later as a missed
    reminder."""
    body = await call(mcp, "create_reminder", title="bins", due_at="in 2 hours")
    assert body["reminder"]["title"] == "bins"
    assert body["reminder"]["due_at"].endswith("+00:00")


async def test_create_accepts_iso(mcp):
    body = await call(mcp, "create_reminder", title="t",
                      due_at="2026-08-15T09:00:00+00:00")
    assert body["reminder"]["due_at"] == "2026-08-15T09:00:00+00:00"


async def test_create_accepts_a_recurrence(mcp):
    body = await call(mcp, "create_reminder", title="bins",
                      due_at="2026-08-15T09:00:00+00:00",
                      recurrence="FREQ=WEEKLY;BYDAY=TU")
    assert body["reminder"]["recurrence"] == "FREQ=WEEKLY;BYDAY=TU"


async def test_create_reports_an_unparseable_date_actionably(mcp):
    with pytest.raises(ToolError, match="sometime soonish"):
        await call(mcp, "create_reminder", title="t", due_at="sometime soonish")


async def test_create_reports_an_unsupported_recurrence_actionably(mcp):
    with pytest.raises(ToolError, match="FREQ=HOURLY is not supported"):
        await call(mcp, "create_reminder", title="t",
                   due_at="2026-08-15T09:00:00+00:00", recurrence="FREQ=HOURLY")


# --- read -----------------------------------------------------------------

async def test_list_defaults_to_pending(mcp, db):
    seed(db, title="open")
    seed(db, title="closed", status=ReminderStatus.acked.value)
    body = await call(mcp, "list_reminders")
    assert [r["title"] for r in body["reminders"]] == ["open"]


async def test_list_can_ask_for_another_status(mcp, db):
    seed(db, title="closed", status=ReminderStatus.acked.value)
    body = await call(mcp, "list_reminders", status="acked")
    assert [r["title"] for r in body["reminders"]] == ["closed"]


async def test_list_honours_limit(mcp, db):
    seed(db, title="a")
    seed(db, title="b")
    assert len((await call(mcp, "list_reminders", limit=1))["reminders"]) == 1


async def test_get_includes_notification_history(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "get_reminder", reminder_id=reminder_id)
    assert body["reminder"]["id"] == reminder_id
    assert body["notifications"] == []
    assert body["completions"] == []


async def test_get_of_unknown_id_is_actionable(mcp):
    with pytest.raises(ToolError, match="999 not found"):
        await call(mcp, "get_reminder", reminder_id=999)


async def test_search_matches_title_and_note(mcp, db):
    seed(db, title="take the bins out")
    seed(db, title="call mum", note="about the bins")
    seed(db, title="unrelated")
    body = await call(mcp, "search_reminders", query="bins")
    assert len(body["reminders"]) == 2


# --- mutate ---------------------------------------------------------------

async def test_update_changes_only_what_was_given(mcp, db):
    reminder_id = seed(db, title="old", note="keep")
    body = await call(mcp, "update_reminder", reminder_id=reminder_id, title="new")
    assert body["reminder"]["title"] == "new"
    assert body["reminder"]["note"] == "keep"


async def test_update_can_clear_a_recurrence(mcp, db):
    reminder_id = seed(db, recurrence="FREQ=DAILY")
    body = await call(mcp, "update_reminder", reminder_id=reminder_id,
                      clear_recurrence=True)
    assert body["reminder"]["recurrence"] is None


async def test_update_of_a_resolved_reminder_is_actionable(mcp, db):
    reminder_id = seed(db, status=ReminderStatus.acked.value)
    with pytest.raises(ToolError, match="already acked"):
        await call(mcp, "update_reminder", reminder_id=reminder_id, title="x")


async def test_complete_resolves_a_one_shot_reminder(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "complete_reminder", reminder_id=reminder_id)
    assert body["reminder"]["status"] == "acked"


async def test_complete_rolls_a_recurring_series_forward(mcp, db):
    # NOTE: deviates from the brief's literal dates (2026-08-15/16) because
    # `next_occurrence` walks a schedule-anchored series forward until it is
    # strictly after `now` (app/logic.py) — with the suite's real wall clock
    # now past 2026-08-15, a hardcoded "day after" assertion would be stale.
    # Anchored to utcnow() instead so the assertion still checks "rolls
    # forward by exactly one day" regardless of when the suite runs.
    from app.timeutil import utcnow
    anchor = utcnow().replace(second=0, microsecond=0) - timedelta(hours=2)
    reminder_id = seed(db, due_at=anchor, recurrence="FREQ=DAILY")
    body = await call(mcp, "complete_reminder", reminder_id=reminder_id)
    assert body["reminder"]["status"] == "pending"
    expected = anchor + timedelta(days=1)
    assert body["reminder"]["due_at"].startswith(expected.strftime("%Y-%m-%dT%H:%M"))


async def test_complete_twice_is_actionable(mcp, db):
    reminder_id = seed(db)
    await call(mcp, "complete_reminder", reminder_id=reminder_id)
    with pytest.raises(ToolError, match="already acked"):
        await call(mcp, "complete_reminder", reminder_id=reminder_id)


async def test_snooze_uses_the_configured_default(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "snooze_reminder", reminder_id=reminder_id)
    assert body["reminder"]["snooze_count"] == 1


async def test_snooze_accepts_a_natural_duration(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "snooze_reminder", reminder_id=reminder_id, duration="2h")
    assert body["reminder"]["snooze_count"] == 1


async def test_snooze_reports_an_unreadable_duration(mcp, db):
    reminder_id = seed(db)
    with pytest.raises(ToolError):
        await call(mcp, "snooze_reminder", reminder_id=reminder_id, duration="in a bit")


async def test_delete_removes_the_reminder(mcp, db):
    reminder_id = seed(db)
    body = await call(mcp, "delete_reminder", reminder_id=reminder_id)
    assert body["deleted"] == reminder_id
    with pytest.raises(ToolError, match="not found"):
        await call(mcp, "get_reminder", reminder_id=reminder_id)


async def test_delete_of_unknown_id_is_actionable(mcp):
    with pytest.raises(ToolError, match="not found"):
        await call(mcp, "delete_reminder", reminder_id=999)


# --- digest ---------------------------------------------------------------

async def test_whats_due_buckets_the_work(mcp, db):
    from app.timeutil import utcnow
    now = utcnow()
    seed(db, title="late", due_at=now - timedelta(hours=2))
    seed(db, title="next week", due_at=now + timedelta(days=6))

    body = await call(mcp, "whats_due", window="week")

    assert [r["title"] for r in body["overdue"]] == ["late"]
    assert [r["title"] for r in body["upcoming"]] == ["next week"]


async def test_whats_due_defaults_to_today(mcp, db):
    from app.timeutil import utcnow
    seed(db, title="next week", due_at=utcnow() + timedelta(days=6))
    assert (await call(mcp, "whats_due"))["upcoming"] == []
