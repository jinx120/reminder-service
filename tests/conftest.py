import pytest
from fastapi.testclient import TestClient

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
