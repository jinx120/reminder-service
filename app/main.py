import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app.bot import build_application, send_reminder_message
from app.config import load_settings
from app.db import Database
from app.errors import (
    InvalidField,
    InvalidRecurrence,
    InvalidTime,
    ReminderNotFound,
    ReminderNotPending,
    ServiceError,
    SnoozeLimitReached,
)
from app.mcp_server import build_mcp
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

# The single mapping from domain error to HTTP status. Starlette resolves a
# handler by walking the exception's MRO, so registering ServiceError once
# covers every subclass, including ones added later.
ERROR_STATUS = {
    ReminderNotFound: 404,
    ReminderNotPending: 409,
    SnoozeLimitReached: 409,
    InvalidRecurrence: 422,
    InvalidTime: 422,
    InvalidField: 422,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def handle_service_error(request, exc: ServiceError):
        return JSONResponse(
            status_code=ERROR_STATUS.get(type(exc), 400),
            content={"detail": str(exc)},
        )


def mount_mcp(app: FastAPI) -> None:
    """Register the MCP connector at exactly /mcp.

    streamable_http_app() is called only for its side effect: it constructs
    mcp.session_manager, which the ASGI app below needs. DNS-rebinding
    protection must be off or every request — TestClient and Funnel alike —
    is rejected with 421 for an "invalid" Host header.

    A Route, not a Mount: Mount never matches its bare prefix, so /mcp would
    307 to /mcp/ and the connector URL would depend on a trailing slash.
    """
    mcp = build_mcp(app.state.db, app.state.settings)
    mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.state.mcp = mcp
    app.router.routes.append(
        Route("/mcp", endpoint=StreamableHTTPASGIApp(mcp.session_manager))
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    db: Database = app.state.db

    if settings.bot_enabled:
        telegram_app = build_application(settings, db)
        await telegram_app.initialize()
        await telegram_app.start()
        # drop_pending_updates avoids replaying stale taps from while we were down.
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        sender = partial(
            send_reminder_message,
            telegram_app.bot,
            settings.chat_id,
            tz=settings.timezone,
            snooze_min=settings.default_snooze_min,
        )
        app.state.tg = telegram_app
        logger.info("telegram bot polling; authorised chat id %s", settings.chat_id)
    else:
        app.state.tg = None
        sender = log_sender
        logger.warning(
            "BOT_TOKEN/CHAT_ID not set — Telegram disabled; "
            "reminders will be logged instead of sent"
        )

    # The zone is silently wrong by default on a non-UTC deployment: it moves every
    # displayed time, every parsed phrase, and quiet hours together, so nothing looks
    # broken locally. Log it so a stale .env is visible at boot.
    logger.info(
        "timezone %s; quiet hours %s",
        settings.timezone,
        f"{settings.quiet_hours_start}-{settings.quiet_hours_end}"
        if settings.quiet_hours_enabled
        else "disabled",
    )

    scheduler = build_scheduler(db, sender, settings)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("scheduler started, ticking every %ss", settings.tick_seconds)

    try:
        if app.state.mcp is not None:
            async with app.state.mcp.session_manager.run():
                logger.info("MCP connector mounted at /mcp")
                yield
        else:
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

    register_error_handlers(app)
    app.include_router(reminders.router)
    if settings.mcp_enabled:
        mount_mcp(app)
    else:
        app.state.mcp = None
        logger.warning("MCP_ENABLED is false — the /mcp connector is not mounted")
    # Mounted last: StaticFiles owns "/" and would otherwise shadow /api and /mcp.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
