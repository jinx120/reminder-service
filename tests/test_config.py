from app.config import load_settings


def test_defaults_when_env_is_empty(monkeypatch):
    for key in ("BOT_TOKEN", "CHAT_ID", "DB_PATH", "TICK_SECONDS",
                "DEFAULT_RETRY_INTERVAL_MIN", "DEFAULT_MAX_RETRIES"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings()
    assert s.bot_token is None
    assert s.chat_id is None
    assert s.db_path == "data/reminders.db"
    assert s.tick_seconds == 30
    assert s.default_retry_interval_min == 15
    assert s.default_max_retries == 4
    assert s.bot_enabled is False


def test_env_overrides_and_chat_id_is_int(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CHAT_ID", "987654321")
    monkeypatch.setenv("DB_PATH", "/data/x.db")
    monkeypatch.setenv("TICK_SECONDS", "60")
    s = load_settings()
    assert s.bot_token == "123:abc"
    assert s.chat_id == 987654321
    assert s.db_path == "/data/x.db"
    assert s.tick_seconds == 60
    assert s.bot_enabled is True


def test_bot_disabled_when_only_token_present(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.delenv("CHAT_ID", raising=False)
    assert load_settings().bot_enabled is False


def test_empty_string_env_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("CHAT_ID", "")
    s = load_settings()
    assert s.bot_token is None
    assert s.chat_id is None
    assert s.bot_enabled is False
