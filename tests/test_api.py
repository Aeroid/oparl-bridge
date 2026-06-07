"""Tests for the FastAPI OParl endpoints using an in-memory SQLite DB."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oparl_bridge.db.models import AgendaItem, Base, File, Meeting, Organization, Paper
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
    org = Organization(
        id=1, name="Gemeinderat", short_name="GR", organization_type="Beschlussgremium"
    )
    mtg = Meeting(id=100, name="Gemeinderatssitzung", organization_id=1)
    db.add_all([org, mtg])
    db.commit()
    db.close()


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


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


# ---------------------------------------------------------------------------
# UI endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_with_full_data():
    """Org + Meeting + AgendaItem + Paper + File in the test DB."""
    db = _TestSession()
    org = Organization(id=1, name="Gemeinderat")
    mtg = Meeting(id=100, name="Gemeinderatssitzung", organization_id=1)
    paper = Paper(
        id=42, name="Vorlage zu TOP 1", reference="VO/26/042", paper_type="Beschlussvorlage"
    )
    ai = AgendaItem(id=200, meeting_id=100, paper_id=42, paper_reference="VO/26/042",
                    number="Ö 1", name="Beschlussfassung", public=True, result="ACCEPTED",
                    resolution_text="Einstimmig beschlossen.", vote_text="10:0")
    paper_file = File(id=1, paper_id=42, name="Vorlage.pdf",
                      access_url="https://example.invalid/allris/wicket/resource/doc42.pdf",
                      mime_type="application/pdf")
    ai_file = File(id=2, agenda_item_id=200, name="Anlage.pdf",
                   access_url="https://example.invalid/allris/wicket/resource/doc99.pdf",
                   mime_type="application/pdf")
    db.add_all([org, mtg, paper, ai, paper_file, ai_file])
    db.commit()
    db.close()


def test_ui_all_empty(client):
    r = client.get("/ui/all")
    assert r.status_code == 200
    assert r.json()["meetings"] == []


def test_ui_all_with_data(client, db_with_full_data):
    r = client.get("/ui/all")
    assert r.status_code == 200
    meetings = r.json()["meetings"]
    assert len(meetings) == 1
    m = meetings[0]
    assert m["id"] == 100
    assert m["name"] == "Gemeinderatssitzung"
    assert m["orgName"] == "Gemeinderat"
    assert len(m["items"]) == 1
    item = m["items"][0]
    assert item["number"] == "Ö 1"
    assert item["paperRef"] == "VO/26/042"


def test_ui_meeting_not_found(client):
    r = client.get("/ui/meeting/999")
    assert r.status_code == 404


def test_ui_meeting_with_data(client, db_with_full_data):
    r = client.get("/ui/meeting/100")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 100
    assert data["name"] == "Gemeinderatssitzung"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["result"] == "ACCEPTED"
    assert item["resolutionText"] == "Einstimmig beschlossen."
    assert item["paper"]["reference"] == "VO/26/042"
    assert len(item["paper"]["files"]) == 1
    assert item["paper"]["files"][0]["name"] == "Vorlage.pdf"
    assert len(item["files"]) == 1
    assert item["files"][0]["name"] == "Anlage.pdf"
