import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace-only as unset."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean like true/false, got {value!r}")


def _env_timezone(name: str, default: str) -> str:
    """Validate an IANA zone name at startup.

    Failing fast here is deliberate: silently falling back to UTC would move
    every reminder by hours with no visible symptom.
    """
    value = _env(name) or default
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{name} is not a valid IANA timezone: {value!r}") from exc
    return value


def _env_hhmm(name: str) -> time | None:
    value = _env(name)
    if value is None:
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError as exc:
        raise ValueError(f"{name} must be HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    chat_id: int | None
    db_path: str
    tick_seconds: int
    default_retry_interval_min: int
    default_max_retries: int
    timezone: str
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    default_snooze_min: int
    max_snoozes: int
    mcp_enabled: bool

    @property
    def bot_enabled(self) -> bool:
        return self.bot_token is not None and self.chat_id is not None

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def quiet_hours_enabled(self) -> bool:
        return self.quiet_hours_start is not None and self.quiet_hours_end is not None


def load_settings() -> Settings:
    chat_id = _env("CHAT_ID")
    quiet_start = _env_hhmm("QUIET_HOURS_START")
    quiet_end = _env_hhmm("QUIET_HOURS_END")
    if (quiet_start is None) != (quiet_end is None):
        # A half-configured window has no defensible interpretation, and
        # guessing one would silence reminders for an unpredictable stretch.
        raise ValueError(
            "QUIET_HOURS_START and QUIET_HOURS_END must both be set or both be unset"
        )

    return Settings(
        bot_token=_env("BOT_TOKEN"),
        chat_id=int(chat_id) if chat_id is not None else None,
        db_path=_env("DB_PATH") or "data/reminders.db",
        tick_seconds=_env_int("TICK_SECONDS", 30),
        default_retry_interval_min=_env_int("DEFAULT_RETRY_INTERVAL_MIN", 15),
        default_max_retries=_env_int("DEFAULT_MAX_RETRIES", 4),
        timezone=_env_timezone("TIMEZONE", "UTC"),
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        default_snooze_min=_env_int("DEFAULT_SNOOZE_MIN", 15),
        max_snoozes=_env_int("MAX_SNOOZES", 20),
        mcp_enabled=_env_bool("MCP_ENABLED", True),
    )
