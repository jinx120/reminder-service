from datetime import timedelta

from app.timeutil import as_utc_iso, utcnow


def create(client, **overrides):
    payload = {
        "title": "Take pills",
        "note": "the blue ones",
        "due_at": "2026-08-12T09:00:00+00:00",
        "retry_interval_min": 15,
        "max_retries": 4,
    }
    payload.update(overrides)
    return client.post("/api/reminders", json=payload)


def test_create_returns_the_stored_reminder(client):
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["title"] == "Take pills"
    assert body["note"] == "the blue ones"
    assert body["status"] == "pending"
    assert body["retry_count"] == 0
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_create_normalises_a_non_utc_due_at_to_utc(client):
    body = create(client, due_at="2026-08-12T14:00:00+05:00").json()
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_create_applies_defaults_for_optional_fields(client):
    response = client.post("/api/reminders",
                           json={"title": "bare", "due_at": "2026-08-12T09:00:00+00:00"})
    body = response.json()
    assert body["retry_interval_min"] == 15
    assert body["max_retries"] == 4
    assert body["note"] is None


def test_create_rejects_an_empty_title(client):
    assert create(client, title="").status_code == 422


def test_create_rejects_a_zero_retry_interval(client):
    assert create(client, retry_interval_min=0).status_code == 422


def test_list_returns_newest_due_first(client):
    create(client, title="later", due_at="2026-08-12T18:00:00+00:00")
    create(client, title="sooner", due_at="2026-08-12T09:00:00+00:00")
    titles = [r["title"] for r in client.get("/api/reminders").json()]
    assert titles == ["sooner", "later"]


def test_list_filters_by_status(client):
    create(client, title="a")
    create(client, title="b")
    client.patch("/api/reminders/2", json={"title": "b2"})

    pending = client.get("/api/reminders", params={"status": "pending"}).json()
    assert len(pending) == 2
    assert client.get("/api/reminders", params={"status": "acked"}).json() == []


def test_list_rejects_an_unknown_status(client):
    assert client.get("/api/reminders", params={"status": "bogus"}).status_code == 422


def test_detail_includes_notification_history(client):
    create(client)
    body = client.get("/api/reminders/1").json()
    assert body["title"] == "Take pills"
    assert body["notifications"] == []


def test_detail_of_unknown_id_is_404(client):
    assert client.get("/api/reminders/999").status_code == 404


def test_patch_updates_only_the_supplied_fields(client):
    create(client)
    body = client.patch("/api/reminders/1", json={"title": "New title"}).json()
    assert body["title"] == "New title"
    assert body["note"] == "the blue ones"
    assert body["retry_interval_min"] == 15


def test_patch_normalises_due_at_to_utc(client):
    create(client)
    body = client.patch("/api/reminders/1",
                        json={"due_at": "2026-08-12T14:00:00+05:00"}).json()
    assert body["due_at"] == "2026-08-12T09:00:00+00:00"


def test_patch_is_rejected_once_the_reminder_is_resolved(client, db):
    from app.models import Reminder, ReminderStatus

    create(client)
    with db.session() as s:
        reminder = s.get(Reminder, 1)
        reminder.status = ReminderStatus.acked.value
        s.add(reminder)
        s.commit()

    response = client.patch("/api/reminders/1", json={"title": "nope"})
    assert response.status_code == 409


def test_patch_of_unknown_id_is_404(client):
    assert client.patch("/api/reminders/999", json={"title": "x"}).status_code == 404


def test_delete_removes_the_reminder_and_its_notifications(client, db):
    from app.models import Notification

    create(client)
    with db.session() as s:
        s.add(Notification(reminder_id=1))
        s.commit()

    assert client.delete("/api/reminders/1").status_code == 204
    assert client.get("/api/reminders/1").status_code == 404
    with db.session() as s:
        from sqlmodel import select
        assert s.exec(select(Notification)).all() == []


def test_delete_of_unknown_id_is_404(client):
    assert client.delete("/api/reminders/999").status_code == 404


def test_healthz_reports_ok(client):
    assert client.get("/api/healthz").json()["status"] == "ok"


def test_create_accepts_a_natural_language_due_at(client):
    body = create(client, due_at="in 2 hours").json()
    assert body["due_at"].endswith("+00:00")


def test_create_rejects_an_unparseable_due_at(client):
    response = create(client, due_at="sometime soonish")
    assert response.status_code == 422
    assert "sometime soonish" in response.json()["detail"]


def test_create_accepts_a_recurrence(client):
    body = create(client, recurrence="FREQ=WEEKLY;BYDAY=TU").json()
    assert body["recurrence"] == "FREQ=WEEKLY;BYDAY=TU"
    assert body["recur_from"] == "schedule"
    assert body["snooze_count"] == 0


def test_create_rejects_an_unsupported_recurrence(client):
    response = create(client, recurrence="FREQ=HOURLY")
    assert response.status_code == 422
    assert "FREQ" in response.json()["detail"]


def test_complete_marks_a_one_shot_reminder_done(client):
    reminder_id = create(client).json()["id"]
    response = client.post(f"/api/reminders/{reminder_id}/complete")
    assert response.status_code == 200
    assert response.json()["status"] == "acked"


def test_complete_rolls_a_recurring_reminder_forward(client):
    # Anchored 2 days ahead of real now (rather than a fixed literal) so the
    # schedule-anchored rollover in app.logic.next_occurrence — which walks
    # forward to max(previous_due, now) — lands exactly one day after
    # due_at regardless of what day the suite happens to run on.
    due_at = (utcnow() + timedelta(days=2)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    created = create(client, due_at=as_utc_iso(due_at), recurrence="FREQ=DAILY").json()
    body = client.post(f"/api/reminders/{created['id']}/complete").json()
    assert body["status"] == "pending"
    assert body["due_at"] == as_utc_iso(due_at + timedelta(days=1))


def test_complete_of_unknown_id_is_404(client):
    assert client.post("/api/reminders/999/complete").status_code == 404


def test_complete_twice_is_409(client):
    reminder_id = create(client).json()["id"]
    client.post(f"/api/reminders/{reminder_id}/complete")
    response = client.post(f"/api/reminders/{reminder_id}/complete")
    assert response.status_code == 409
    assert "acked" in response.json()["detail"]


def test_snooze_without_a_body_uses_the_default(client):
    reminder_id = create(client).json()["id"]
    body = client.post(f"/api/reminders/{reminder_id}/snooze").json()
    assert body["status"] == "pending"
    assert body["snooze_count"] == 1


def test_snooze_accepts_a_duration(client):
    reminder_id = create(client).json()["id"]
    body = client.post(f"/api/reminders/{reminder_id}/snooze",
                       json={"duration": "2h"}).json()
    assert body["snooze_count"] == 1


def test_snooze_rejects_gibberish(client):
    reminder_id = create(client).json()["id"]
    response = client.post(f"/api/reminders/{reminder_id}/snooze",
                           json={"duration": "in a bit"})
    assert response.status_code == 422


def test_detail_includes_completions(client):
    created = create(client, due_at="2026-08-12T09:00:00+00:00",
                     recurrence="FREQ=DAILY").json()
    client.post(f"/api/reminders/{created['id']}/complete")

    completions = client.get(f"/api/reminders/{created['id']}").json()["completions"]
    assert len(completions) == 1
    assert completions[0]["outcome"] == "completed"
    assert completions[0]["scheduled_for"] == "2026-08-12T09:00:00+00:00"


def test_config_exposes_what_the_frontend_needs(client):
    body = client.get("/api/config").json()
    assert body["timezone"] == "UTC"
    assert body["default_snooze_min"] == 15
    assert body["max_snoozes"] == 20
    assert body["quiet_hours_start"] is None
    assert body["quiet_hours_end"] is None
    assert body["server_time"].endswith("+00:00")


def test_patch_refuses_to_null_a_required_field(client):
    reminder_id = create(client).json()["id"]
    response = client.patch(f"/api/reminders/{reminder_id}", json={"recur_from": None})
    assert response.status_code == 422


def test_read_schema_is_backward_compatible(client):
    """The old dashboard must keep working across the deploy."""
    body = create(client).json()
    assert {"id", "title", "note", "due_at", "retry_interval_min", "max_retries",
            "status", "retry_count", "last_sent_at", "created_at"} <= set(body)
