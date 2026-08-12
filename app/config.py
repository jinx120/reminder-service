import os
from dataclasses import dataclass


def _env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace-only as unset."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    chat_id: int | None
    db_path: str
    tick_seconds: int
    default_retry_interval_min: int
    default_max_retries: int

    @property
    def bot_enabled(self) -> bool:
        return self.bot_token is not None and self.chat_id is not None


def load_settings() -> Settings:
    chat_id = _env("CHAT_ID")
    return Settings(
        bot_token=_env("BOT_TOKEN"),
        chat_id=int(chat_id) if chat_id is not None else None,
        db_path=_env("DB_PATH") or "data/reminders.db",
        tick_seconds=_env_int("TICK_SECONDS", 30),
        default_retry_interval_min=_env_int("DEFAULT_RETRY_INTERVAL_MIN", 15),
        default_max_retries=_env_int("DEFAULT_MAX_RETRIES", 4),
    )
