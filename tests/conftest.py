import pytest

from app.db import Database


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_all()
    return database


@pytest.fixture
def session(db):
    with db.session() as s:
        yield s
