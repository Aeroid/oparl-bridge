"""Tests for OParl 1.1 API endpoints."""

import pytest

from oparl_bridge.db.models import (
    AgendaItem,
    File,
    Meeting,
    Membership,
    Organization,
    Paper,
    Person,
)

# conftest.py provides: client, app, setup_db (autouse), _TestSession via conftest
from tests.conftest import _TestSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_with_data():
    db = _TestSession()
    org = Organization(id=1, name="Gemeinderat", short_name="GR", organization_type="Beschlussgremium")
    mtg = Meeting(id=100, name="Gemeinderatssitzung", organization_id=1)
    db.add_all([org, mtg])
    db.commit()
    db.close()


@pytest.fixture()
def db_with_full_data():
    db = _TestSession()
    org = Organization(id=1, name="Gemeinderat")
    mtg = Meeting(id=100, name="Gemeinderatssitzung", organization_id=1)
    paper = Paper(id=42, name="Vorlage zu TOP 1", reference="VO/26/042", paper_type="Beschlussvorlage")
    ai = AgendaItem(
        id=200, meeting_id=100, paper_id=42, paper_reference="VO/26/042",
        number="Ö 1", name="Beschlussfassung", public=True, result="ACCEPTED",
        resolution_text="Einstimmig beschlossen.", vote_text="10:0",
    )
    paper_file = File(
        id=1, paper_id=42, name="Vorlage.pdf",
        access_url="https://example.invalid/allris/wicket/resource/doc42.pdf",
        mime_type="application/pdf",
    )
    ai_file = File(
        id=2, agenda_item_id=200, name="Anlage.pdf",
        access_url="https://example.invalid/allris/wicket/resource/doc99.pdf",
        mime_type="application/pdf",
    )
    db.add_all([org, mtg, paper, ai, paper_file, ai_file])
    db.commit()
    db.close()


@pytest.fixture()
def db_with_persons(db_with_full_data):
    from datetime import datetime
    db = _TestSession()
    person = Person(id=10, name="Ada Lovelace", scraped_at=datetime(2024, 1, 1))
    membership = Membership(id=1, person_id=10, organization_id=1, role="Mitglied")
    db.add_all([person, membership])
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# System / Body
# ---------------------------------------------------------------------------

def test_system_endpoint(client):
    r = client.get("/oparl/v1.1/")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/System"
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
    assert len(r.json()["data"]) == 1


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------

def test_organizations_empty(client):
    assert client.get("/oparl/v1.1/body/1/organizations").json()["data"] == []


def test_organization_not_found(client):
    assert client.get("/oparl/v1.1/organization/999").status_code == 404


def test_organizations_with_data(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/organizations")
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Gemeinderat"
    assert data[0]["type"] == "https://schema.oparl.org/1.1/Organization"


def test_organization_detail_has_md_fields(client, db_with_data):
    r = client.get("/oparl/v1.1/organization/1")
    assert r.status_code == 200
    data = r.json()
    assert "x-markdownUrl" in data
    assert "/md/gremien/1" in data["x-markdownUrl"]
    assert "link" in r.headers


def test_organizations_list_has_md_link_header(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/organizations")
    assert "link" in r.headers
    assert "/md/" in r.headers["link"]


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

def test_meeting_not_found(client):
    assert client.get("/oparl/v1.1/meeting/999").status_code == 404


def test_meetings_with_data(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/meetings")
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Gemeinderatssitzung"


def test_meetings_filtered_by_organization(client, db_with_data):
    assert len(client.get("/oparl/v1.1/body/1/meetings?organization=1").json()["data"]) == 1
    assert client.get("/oparl/v1.1/body/1/meetings?organization=99").json()["data"] == []


def test_meeting_detail_has_md_fields(client, db_with_data):
    r = client.get("/oparl/v1.1/meeting/100")
    assert r.status_code == 200
    data = r.json()
    assert "x-markdownUrl" in data
    assert "/md/sitzungen/100" in data["x-markdownUrl"]


def test_meetings_list_has_md_link_header(client, db_with_data):
    r = client.get("/oparl/v1.1/body/1/meetings")
    assert "link" in r.headers
    assert "/md/sitzungen/" in r.headers["link"]


# ---------------------------------------------------------------------------
# AgendaItem
# ---------------------------------------------------------------------------

def test_agendaitem_not_found(client):
    assert client.get("/oparl/v1.1/agendaitem/9999").status_code == 404


def test_agendaitem_found(client, db_with_full_data):
    r = client.get("/oparl/v1.1/agendaitem/200")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/AgendaItem"
    assert data["name"] == "Beschlussfassung"
    assert data["number"] == "Ö 1"
    assert data["result"] == "ACCEPTED"


# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------

def test_paper_list_empty(client):
    assert client.get("/oparl/v1.1/body/1/papers").json()["data"] == []


def test_paper_list_with_data(client, db_with_full_data):
    data = client.get("/oparl/v1.1/body/1/papers").json()["data"]
    assert len(data) == 1
    assert data[0]["type"] == "https://schema.oparl.org/1.1/Paper"
    assert data[0]["name"] == "Vorlage zu TOP 1"


def test_paper_not_found(client):
    assert client.get("/oparl/v1.1/paper/9999").status_code == 404


def test_paper_found(client, db_with_full_data):
    r = client.get("/oparl/v1.1/paper/42")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/Paper"
    assert data["reference"] == "VO/26/042"
    assert "x-markdownUrl" in data
    assert "link" in r.headers


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------

def test_file_not_found(client):
    assert client.get("/oparl/v1.1/file/9999").status_code == 404


def test_file_found(client, db_with_full_data):
    r = client.get("/oparl/v1.1/file/1")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "https://schema.oparl.org/1.1/File"
    assert data["name"] == "Vorlage.pdf"


# ---------------------------------------------------------------------------
# Person / Membership
# ---------------------------------------------------------------------------

def test_persons_empty(client):
    assert client.get("/oparl/v1.1/body/1/persons").json()["data"] == []


def test_person_not_found(client):
    assert client.get("/oparl/v1.1/person/9999").status_code == 404


def test_membership_not_found(client):
    assert client.get("/oparl/v1.1/membership/9999").status_code == 404


def test_persons_with_data(client, db_with_persons):
    data = client.get("/oparl/v1.1/body/1/persons").json()["data"]
    assert len(data) == 1
    assert data[0]["type"] == "https://schema.oparl.org/1.1/Person"
    assert data[0]["name"] == "Ada Lovelace"


def test_person_found(client, db_with_persons):
    r = client.get("/oparl/v1.1/person/10")
    assert r.status_code == 200
    assert r.json()["name"] == "Ada Lovelace"


def test_membership_found(client, db_with_persons):
    r = client.get("/oparl/v1.1/membership/1")
    assert r.status_code == 200
    assert r.json()["role"] == "Mitglied"
