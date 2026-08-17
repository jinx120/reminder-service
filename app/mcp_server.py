"""The MCP connector.

Every MCP request enters through this module and nothing else, which is what
keeps a future Authorization-header check to one function rather than a
scattering of edits (spec §4). Business rules live in app/service.py; this
file only translates between MCP tool calls and that layer.
"""

from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlmodel import select

from app import service
from app.config import Settings
from app.db import Database
from app.errors import ServiceError
from app.models import Completion, Notification, Reminder
from app.timeutil import as_local_iso, utcnow


@contextmanager
def _work(db: Database):
    """A session plus domain-error translation, in one place.

    Domain errors carry an actionable message ("reminder 12 is already acked
    and cannot be edited"); re-raising them as ToolError is what puts that
    message in front of the model instead of a stack trace.
    """
    try:
        with db.session() as session:
            yield session
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


def _reminder_dict(reminder: Reminder, tz: str) -> dict:
    """Absolute times, rendered in the configured zone with an explicit offset.

    Unambiguous to a machine and readable in the terms the user thinks in.
    """
    return {
        "id": reminder.id,
        "title": reminder.title,
        "note": reminder.note,
        "due_at": as_local_iso(reminder.due_at, tz),
        "status": reminder.status,
        "recurrence": reminder.recurrence,
        "recur_from": reminder.recur_from,
        "snooze_count": reminder.snooze_count,
        "retry_count": reminder.retry_count,
        "max_retries": reminder.max_retries,
        "retry_interval_min": reminder.retry_interval_min,
        "last_sent_at": as_local_iso(reminder.last_sent_at, tz),
        "created_at": as_local_iso(reminder.created_at, tz),
    }


def build_mcp(db: Database, settings: Settings) -> MCPServer:
    tz = settings.timezone

    def envelope(**payload) -> dict:
        """Every result carries the zone and a *fresh* local clock reading.

        The timezone also appears in each tool description, but the current
        time deliberately does not: a description is built once at startup and
        an embedded clock would be stale — and actively misleading — within
        minutes.
        """
        return {"timezone": tz, "server_time": as_local_iso(utcnow(), tz), **payload}

    def one(reminder: Reminder) -> dict:
        return envelope(reminder=_reminder_dict(reminder, tz))

    def many(reminders: list[Reminder], **extra) -> dict:
        return envelope(reminders=[_reminder_dict(r, tz) for r in reminders], **extra)

    mcp = MCPServer(
        name="reminders",
        instructions=(
            f"Reminder service. All times are in {tz}. Relative phrasings like "
            "'tomorrow at 9am' or 'in 2 hours' are resolved server-side in that "
            "zone; every result echoes the resolved absolute time, so check it "
            "before confirming to the user."
        ),
    )

    when_help = (
        f"ISO-8601 or natural language ('tomorrow at 9am', 'in 2 hours', "
        f"'friday 18:00'), resolved in {tz}."
    )
    recurrence_help = (
        "Optional RRULE subset: FREQ=DAILY|WEEKLY|MONTHLY|YEARLY, optional "
        "INTERVAL=<n>, and BYDAY=MO,TU,... for weekly rules only. "
        "Anything else is rejected."
    )

    @mcp.tool(description=f"Create a reminder. due_at: {when_help} {recurrence_help}")
    def create_reminder(
        title: str,
        due_at: str,
        note: str | None = None,
        recurrence: str | None = None,
        recur_from: str = "schedule",
        retry_interval_min: int = 15,
        max_retries: int = 4,
    ) -> dict:
        with _work(db) as session:
            return one(
                service.create_reminder(
                    session,
                    title=title,
                    due_at=due_at,
                    note=note,
                    recurrence=recurrence,
                    recur_from=recur_from,
                    retry_interval_min=retry_interval_min,
                    max_retries=max_retries,
                    tz=tz,
                )
            )

    @mcp.tool(
        description=f"List reminders, pending by default. Times are in {tz}."
    )
    def list_reminders(status: str = "pending", limit: int = 50) -> dict:
        with _work(db) as session:
            return many(service.list_reminders(session, status=status, limit=limit))

    @mcp.tool(
        description=f"One reminder with its notification and completion history. Times are in {tz}."
    )
    def get_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            reminder = service.get_reminder(session, reminder_id)
            notifications = session.exec(
                select(Notification)
                .where(Notification.reminder_id == reminder_id)
                .order_by(Notification.sent_at, Notification.id)
            ).all()
            completions = session.exec(
                select(Completion)
                .where(Completion.reminder_id == reminder_id)
                .order_by(Completion.completed_at, Completion.id)
            ).all()
            return envelope(
                reminder=_reminder_dict(reminder, tz),
                notifications=[
                    {
                        "id": n.id,
                        "sent_at": as_local_iso(n.sent_at, tz),
                        "acked_at": as_local_iso(n.acked_at, tz),
                    }
                    for n in notifications
                ],
                completions=[
                    {
                        "id": c.id,
                        "scheduled_for": as_local_iso(c.scheduled_for, tz),
                        "completed_at": as_local_iso(c.completed_at, tz),
                        "outcome": c.outcome,
                    }
                    for c in completions
                ],
            )

    @mcp.tool(
        description=(
            f"Edit a pending reminder; only the fields you pass change. "
            f"due_at: {when_help} Pass clear_recurrence=true to turn a "
            f"repeating reminder back into a one-shot."
        )
    )
    def update_reminder(
        reminder_id: int,
        title: str | None = None,
        note: str | None = None,
        due_at: str | None = None,
        recurrence: str | None = None,
        recur_from: str | None = None,
        retry_interval_min: int | None = None,
        max_retries: int | None = None,
        clear_recurrence: bool = False,
    ) -> dict:
        changes = {
            key: value
            for key, value in {
                "title": title,
                "note": note,
                "due_at": due_at,
                "recurrence": recurrence,
                "recur_from": recur_from,
                "retry_interval_min": retry_interval_min,
                "max_retries": max_retries,
            }.items()
            if value is not None
        }
        if clear_recurrence:
            # An omitted argument and an explicit null are indistinguishable
            # over JSON-RPC, so clearing needs its own flag.
            changes["recurrence"] = None

        with _work(db) as session:
            return one(service.update_reminder(session, reminder_id, changes, tz=tz))

    @mcp.tool(
        description=(
            "Mark a reminder done. A repeating reminder rolls forward to its "
            "next occurrence instead of closing."
        )
    )
    def complete_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            return one(service.complete_reminder(session, reminder_id, tz=tz))

    @mcp.tool(
        description=(
            f"Push a reminder later. duration accepts '30m', '2h', or a phrase "
            f"like 'tomorrow at 9am' resolved in {tz}. Omit it to use the "
            f"configured default of {settings.default_snooze_min} minutes."
        )
    )
    def snooze_reminder(reminder_id: int, duration: str | None = None) -> dict:
        with _work(db) as session:
            return one(
                service.snooze_reminder(
                    session,
                    reminder_id,
                    duration=duration,
                    default_minutes=settings.default_snooze_min,
                    max_snoozes=settings.max_snoozes,
                    tz=tz,
                )
            )

    @mcp.tool(description="Delete a reminder permanently, with its history.")
    def delete_reminder(reminder_id: int) -> dict:
        with _work(db) as session:
            service.delete_reminder(session, reminder_id)
            return envelope(deleted=reminder_id)

    @mcp.tool(description="Substring search over reminder titles and notes.")
    def search_reminders(query: str, status: str | None = None) -> dict:
        with _work(db) as session:
            return many(service.search_reminders(session, query, status=status))

    @mcp.tool(
        description=(
            f"What needs attention, split into overdue / due today / upcoming. "
            f"window accepts 'today' (default), 'tomorrow', 'week', 'all', or a "
            f"phrase like 'in 3 days'. Day boundaries are in {tz}."
        )
    )
    def whats_due(window: str = "today") -> dict:
        with _work(db) as session:
            digest = service.due_digest(session, window=window, tz=tz)
            return envelope(
                horizon=as_local_iso(digest["horizon"], tz),
                overdue=[_reminder_dict(r, tz) for r in digest["overdue"]],
                due_today=[_reminder_dict(r, tz) for r in digest["due_today"]],
                upcoming=[_reminder_dict(r, tz) for r in digest["upcoming"]],
            )

    return mcp
