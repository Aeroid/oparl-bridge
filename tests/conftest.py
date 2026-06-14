"""Shared test fixtures for all test modules."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oparl_bridge.db.models import Base
from oparl_bridge.db.session import get_db
from oparl_bridge.main import create_app

# Single in-memory engine shared across all tests; StaticPool keeps one connection.
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test, drop them after."""
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def app():
    """Full app (all frontends) with DB overridden to the in-memory engine."""
    a = create_app()
    a.dependency_overrides[get_db] = _override_get_db
    return a


@pytest.fixture()
def client(app):
    return TestClient(app)
