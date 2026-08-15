import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bot import build_application, send_reminder_message
from app.config import load_settings
from app.db import Database
from app.migrations import migrate
from app.routers import reminders
from app.scheduler import build_scheduler, log_sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("reminder")

# python-telegram-bot issues every API call through httpx, which logs the full
# request URL at INFO — and that URL embeds BOT_TOKEN. Without this the bot
# token ends up in plain text in `docker compose logs`.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Load .env for local (non-Docker) runs. Compose supplies the environment
# itself via env_file, and load_dotenv never overrides an already-set variable,
# so this is a no-op in the container. Deliberately not in config.py: that
# would pull a real .env into the config tests.
load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    db: Database = app.state.db

    if settings.bot_enabled:
        telegram_app = build_application(settings.bot_token, settings.chat_id, db)
        await telegram_app.initialize()
        await telegram_app.start()
        # drop_pending_updates avoids replaying stale taps from while we were down.
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        sender = partial(send_reminder_message, telegram_app.bot, settings.chat_id)
        app.state.tg = telegram_app
        logger.info("telegram bot polling; authorised chat id %s", settings.chat_id)
    else:
        app.state.tg = None
        sender = log_sender
        logger.warning(
            "BOT_TOKEN/CHAT_ID not set — Telegram disabled; "
            "reminders will be logged instead of sent"
        )

    scheduler = build_scheduler(db, sender, settings.tick_seconds)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("scheduler started, ticking every %ss", settings.tick_seconds)

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if app.state.tg is not None:
            await app.state.tg.updater.stop()
            await app.state.tg.stop()
            await app.state.tg.shutdown()
        logger.info("shutdown complete")


def create_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app.

    `db` is injected by tests; production passes nothing and gets a file-backed
    database at `settings.db_path`.
    """
    settings = load_settings()
    app = FastAPI(title="Reminder Service", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db or Database(settings.db_path)
    app.state.db.create_all()
    # create_all() adds missing tables but never missing columns, so an
    # existing production database needs this explicit step. A failure here
    # aborts startup deliberately.
    migrate(app.state.db.engine)

    app.include_router(reminders.router)
    # Mounted last: StaticFiles owns "/" and would otherwise shadow /api.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
