"""Tests for the browser SPA / UI endpoints."""

import pytest

from oparl_bridge.db.models import AgendaItem, File, Meeting, Organization, Paper
from tests.conftest import _TestSession


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


# ---------------------------------------------------------------------------
# Static / SPA root
# ---------------------------------------------------------------------------

def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /ui/all
# ---------------------------------------------------------------------------

def test_ui_all_empty(client):
    assert client.get("/ui/all").json()["meetings"] == []


def test_ui_all_with_data(client, db_with_full_data):
    r = client.get("/ui/all")
    assert r.status_code == 200
    meetings = r.json()["meetings"]
    assert len(meetings) == 1
    m = meetings[0]
    assert m["id"] == 100
    assert m["name"] == "Gemeinderatssitzung"
    assert m["orgName"] == "Gemeinderat"
    item = m["items"][0]
    assert item["number"] == "Ö 1"
    assert item["paperRef"] == "VO/26/042"


# ---------------------------------------------------------------------------
# /ui/meeting/{id}
# ---------------------------------------------------------------------------

def test_ui_meeting_not_found(client):
    assert client.get("/ui/meeting/999").status_code == 404


def test_ui_meeting_with_data(client, db_with_full_data):
    r = client.get("/ui/meeting/100")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 100
    assert data["name"] == "Gemeinderatssitzung"
    item = data["items"][0]
    assert item["result"] == "ACCEPTED"
    assert item["resolutionText"] == "Einstimmig beschlossen."
    assert item["paper"]["reference"] == "VO/26/042"
    assert item["paper"]["files"][0]["name"] == "Vorlage.pdf"
    assert item["files"][0]["name"] == "Anlage.pdf"
