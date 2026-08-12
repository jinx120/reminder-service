from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# Importing the models module registers the tables on SQLModel.metadata,
# which is what create_all() reads. Without it create_all() is a no-op.
from app import models  # noqa: F401


class Database:
    """Owns the SQLite engine. Instantiated once in lifespan, per-test in tests."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path == ":memory:":
            # StaticPool keeps every session on the same in-memory connection,
            # otherwise each session would get a fresh, empty database.
            self.engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )

    def create_all(self) -> None:
        SQLModel.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = Session(self.engine)
        try:
            yield session
        finally:
            session.close()
