from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from app.db import Database
from app.models import Notification, Reminder, ReminderStatus
from app.schemas import (
    ReminderCreate,
    ReminderDetail,
    ReminderRead,
    ReminderUpdate,
    to_detail,
    to_read,
)
from app.timeutil import to_utc_naive

router = APIRouter(prefix="/api", tags=["reminders"])


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.db
    with database.session() as session:
        yield session


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/reminders", response_model=ReminderRead, status_code=201)
def create_reminder(payload: ReminderCreate, session: Session = Depends(get_session)):
    reminder = Reminder(
        title=payload.title,
        note=payload.note,
        due_at=to_utc_naive(payload.due_at),
        retry_interval_min=payload.retry_interval_min,
        max_retries=payload.max_retries,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return to_read(reminder)


@router.get("/reminders", response_model=list[ReminderRead])
def list_reminders(
    status: ReminderStatus | None = None,
    session: Session = Depends(get_session),
):
    statement = select(Reminder)
    if status is not None:
        statement = statement.where(Reminder.status == status.value)
    statement = statement.order_by(Reminder.due_at, Reminder.id)
    return [to_read(r) for r in session.exec(statement).all()]


def _get_or_404(session: Session, reminder_id: int) -> Reminder:
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return reminder


@router.get("/reminders/{reminder_id}", response_model=ReminderDetail)
def get_reminder(reminder_id: int, session: Session = Depends(get_session)):
    reminder = _get_or_404(session, reminder_id)
    notifications = session.exec(
        select(Notification)
        .where(Notification.reminder_id == reminder_id)
        .order_by(Notification.sent_at, Notification.id)
    ).all()
    return to_detail(reminder, list(notifications))


@router.patch("/reminders/{reminder_id}", response_model=ReminderRead)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    session: Session = Depends(get_session),
):
    reminder = _get_or_404(session, reminder_id)
    if reminder.status != ReminderStatus.pending.value:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit a reminder that is already {reminder.status}",
        )

    changes = payload.model_dump(exclude_unset=True)
    if "due_at" in changes and changes["due_at"] is not None:
        changes["due_at"] = to_utc_naive(changes["due_at"])
    for field, value in changes.items():
        setattr(reminder, field, value)

    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return to_read(reminder)


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: int, session: Session = Depends(get_session)):
    reminder = _get_or_404(session, reminder_id)
    for notification in session.exec(
        select(Notification).where(Notification.reminder_id == reminder_id)
    ).all():
        session.delete(notification)
    session.delete(reminder)
    session.commit()
    return Response(status_code=204)
