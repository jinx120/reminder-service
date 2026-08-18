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


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def test_mcp_answers_on_the_bare_path_without_redirecting(client):
    """A Mount would 307 /mcp -> /mcp/; connector URLs must not depend on a
    trailing slash."""
    response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 200
    assert "serverInfo" in response.text


def test_mcp_does_not_shadow_the_api(client):
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_mcp_does_not_shadow_static_files(client):
    assert client.get("/").status_code == 200


def test_mcp_can_be_disabled_by_configuration(db, monkeypatch):
    """The escape hatch: drop the sub-app without a code change."""
    monkeypatch.setenv("MCP_ENABLED", "false")
    with TestClient(create_app(db=db)) as disabled_client:
        assert disabled_client.app.state.mcp is None
        # StaticFiles owns "/" and answers everything else with its own 404.
        assert disabled_client.post("/mcp", json=INITIALIZE,
                                    headers=MCP_HEADERS).status_code != 200


def test_startup_logs_the_effective_timezone(monkeypatch, tmp_path, caplog):
    """The zone drives display, NL parsing, and quiet hours, and its default (UTC)
    is silently wrong on any non-UTC host. Log it so a misconfigured deploy is
    visible in the logs instead of showing up as reminders firing hours off."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TIMEZONE", "America/Los_Angeles")

    with caplog.at_level("INFO", logger="reminder"):
        with TestClient(create_app()):
            pass

    assert "America/Los_Angeles" in caplog.text
