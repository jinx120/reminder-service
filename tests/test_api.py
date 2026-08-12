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
