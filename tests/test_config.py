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


from datetime import time

import pytest

NEW_KEYS = ("TIMEZONE", "QUIET_HOURS_START", "QUIET_HOURS_END",
            "DEFAULT_SNOOZE_MIN", "MAX_SNOOZES", "MCP_ENABLED")


@pytest.fixture(autouse=True)
def _clean_new_env(monkeypatch):
    for key in NEW_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_new_settings_default_to_current_behaviour():
    s = load_settings()
    assert s.timezone == "UTC"
    assert s.quiet_hours_start is None
    assert s.quiet_hours_end is None
    assert s.quiet_hours_enabled is False
    assert s.default_snooze_min == 15
    assert s.max_snoozes == 20
    assert s.mcp_enabled is True


def test_timezone_is_parsed_into_a_zoneinfo(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/London")
    s = load_settings()
    assert s.timezone == "Europe/London"
    assert str(s.tzinfo) == "Europe/London"


def test_invalid_timezone_fails_fast(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="TIMEZONE"):
        load_settings()


def test_quiet_hours_are_parsed(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    monkeypatch.setenv("QUIET_HOURS_END", "08:00")
    s = load_settings()
    assert s.quiet_hours_start == time(22, 0)
    assert s.quiet_hours_end == time(8, 0)
    assert s.quiet_hours_enabled is True


def test_malformed_quiet_hours_fails_fast(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "10pm")
    monkeypatch.setenv("QUIET_HOURS_END", "08:00")
    with pytest.raises(ValueError, match="QUIET_HOURS_START"):
        load_settings()


def test_half_configured_quiet_hours_fails_fast(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    with pytest.raises(ValueError, match="both"):
        load_settings()


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("FALSE", False), ("0", False), ("no", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
])
def test_mcp_enabled_accepts_common_boolean_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("MCP_ENABLED", raw)
    assert load_settings().mcp_enabled is expected


def test_snooze_settings_override(monkeypatch):
    monkeypatch.setenv("DEFAULT_SNOOZE_MIN", "5")
    monkeypatch.setenv("MAX_SNOOZES", "3")
    s = load_settings()
    assert s.default_snooze_min == 5
    assert s.max_snoozes == 3
