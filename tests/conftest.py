import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.db import Database
from app.main import create_app


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_all()
    return database


@pytest.fixture
def session(db):
    with db.session() as s:
        yield s


@pytest.fixture
def client(db) -> TestClient:
    with TestClient(create_app(db=db)) as test_client:
        yield test_client


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Default settings with a guaranteed-clean environment.

    Tests that need a different zone or quiet hours build their own with
    `dataclasses.replace(settings, timezone="Europe/London")`.
    """
    for key in ("BOT_TOKEN", "CHAT_ID", "DB_PATH", "TICK_SECONDS",
                "DEFAULT_RETRY_INTERVAL_MIN", "DEFAULT_MAX_RETRIES",
                "TIMEZONE", "QUIET_HOURS_START", "QUIET_HOURS_END",
                "DEFAULT_SNOOZE_MIN", "MAX_SNOOZES", "MCP_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    return load_settings()
