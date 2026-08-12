from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app


def test_app_boots_without_telegram_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").json()["status"] == "ok"


def test_scheduler_runs_even_with_telegram_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    app = create_app()
    with TestClient(app):
        assert app.state.scheduler.running is True
        assert app.state.tg is None


def test_static_dashboard_is_served_at_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_injected_db_still_bypasses_runtime_wiring():
    db = Database(":memory:")
    db.create_all()
    app = create_app(db=db)
    with TestClient(app) as client:
        assert client.get("/api/healthz").status_code == 200
    assert app.state.db is db
