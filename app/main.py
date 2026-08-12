import logging

from fastapi import FastAPI

from app.config import load_settings
from app.db import Database
from app.routers import reminders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("reminder")


def create_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app.

    Passing `db` (as tests do) skips runtime wiring entirely.
    """
    app = FastAPI(title="Reminder Service")
    settings = load_settings()
    app.state.settings = settings
    app.state.db = db or Database(settings.db_path)
    app.state.db.create_all()
    app.include_router(reminders.router)
    return app


app = create_app()
