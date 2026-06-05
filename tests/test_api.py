"""Tests for the FastAPI OParl endpoints using an in-memory SQLite DB."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oparl_bridge.db.models import Base, Meeting, Organization
from oparl_bridge.db.session import get_db
from oparl_bridge.main import app

# StaticPool ensures all connections share the same in-memory SQLite DB
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
    Base.metadata.create_all(bind=_engine)
    app.dependency_overrides[get_db] = _override_get_db
    yield
    Base.metadata.drop_all(bind=_engine)
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db_with_data():
    db = _TestSession()
    org = Organization(id=1, name="Gemeinderat", short_name="GR", organization_type="Beschlussgremium")
    mtg = Meeting(id=100, name="Gemeinderatssitzung", organization_id=1)
    db.add_all([org, mtg])
    db.commit()
    db.close()


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "oparl_endpoint" in r.json()


def test_system_endpoint(client):
    r = client.get("/oparl/v1.1/")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/System"
    assert "oparlVersion" in data
    assert data["oparlVersion"] == "https://schema.oparl.org/1.1/"


def test_body_endpoint(client):
    r = client.get("/oparl/v1.1/body/1")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/Body"
    assert "organization" in data
    assert "meeting" in data
    assert "paper" in data


def test_bodies_list(client):
    r = client.get("/oparl/v1.1/bodies")
    assert r.status_code == 200
    data = r.json()
    assert "data" in data
    assert len(data["data"]) == 1


def test_organizations_empty(client):
    r = client.get("/oparl/v1.1/body/1/organizations")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_organization_not_found(client):
    r = client.get("/oparl/v1.1/organization/999")
    assert r.status_code == 404


def test_organizations_with_data(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/organizations")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Gemeinderat"
    assert data[0]["type"] == "https://schema.oparl.org/1.1/Organization"


def test_meeting_not_found(client):
    r = client.get("/oparl/v1.1/meeting/999")
    assert r.status_code == 404


def test_meetings_with_data(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/meetings")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Gemeinderatssitzung"


def test_meetings_filtered_by_organization(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/meetings?organization=1")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1

    r2 = client.get("/oparl/v1.1/body/1/meetings?organization=99")
    assert r2.status_code == 200
    assert r2.json()["data"] == []
