from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from app import service
from app.config import Settings
from app.db import Database
from app.models import Completion, Notification, ReminderStatus
from app.schemas import (
    ConfigRead,
    ReminderCreate,
    ReminderDetail,
    ReminderRead,
    ReminderUpdate,
    SnoozeRequest,
    to_detail,
    to_read,
)
from app.timeutil import as_local_iso, utcnow

router = APIRouter(prefix="/api", tags=["reminders"])


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.db
    with database.session() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config", response_model=ConfigRead)
def read_config(settings: Settings = Depends(get_settings)):
    """Everything the frontend needs to render times and label its controls."""
    return ConfigRead(
        timezone=settings.timezone,
        default_snooze_min=settings.default_snooze_min,
        max_snoozes=settings.max_snoozes,
        quiet_hours_start=(
            settings.quiet_hours_start.strftime("%H:%M")
            if settings.quiet_hours_start else None
        ),
        quiet_hours_end=(
            settings.quiet_hours_end.strftime("%H:%M")
            if settings.quiet_hours_end else None
        ),
        server_time=as_local_iso(utcnow(), settings.timezone),
    )


@router.post("/reminders", response_model=ReminderRead, status_code=201)
def create_reminder(
    payload: ReminderCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.create_reminder(
            session,
            title=payload.title,
            note=payload.note,
            due_at=payload.due_at,
            recurrence=payload.recurrence,
            recur_from=payload.recur_from,
            retry_interval_min=payload.retry_interval_min,
            max_retries=payload.max_retries,
            tz=settings.timezone,
        )
    )


@router.get("/reminders", response_model=list[ReminderRead])
def list_reminders(
    status: ReminderStatus | None = None,
    session: Session = Depends(get_session),
):
    reminders = service.list_reminders(
        session, status=status.value if status is not None else None
    )
    return [to_read(r) for r in reminders]


@router.get("/reminders/{reminder_id}", response_model=ReminderDetail)
def get_reminder(reminder_id: int, session: Session = Depends(get_session)):
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
    return to_detail(reminder, list(notifications), list(completions))


@router.patch("/reminders/{reminder_id}", response_model=ReminderRead)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.update_reminder(
            session,
            reminder_id,
            payload.model_dump(exclude_unset=True),
            tz=settings.timezone,
        )
    )


@router.post("/reminders/{reminder_id}/complete", response_model=ReminderRead)
def complete_reminder(
    reminder_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.complete_reminder(session, reminder_id, tz=settings.timezone)
    )


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderRead)
def snooze_reminder(
    reminder_id: int,
    payload: SnoozeRequest | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return to_read(
        service.snooze_reminder(
            session,
            reminder_id,
            duration=payload.duration if payload else None,
            default_minutes=settings.default_snooze_min,
            max_snoozes=settings.max_snoozes,
            tz=settings.timezone,
        )
    )


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: int, session: Session = Depends(get_session)):
    service.delete_reminder(session, reminder_id)
    return Response(status_code=204)
