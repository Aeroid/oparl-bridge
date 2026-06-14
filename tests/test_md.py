"""Tests for Markdown endpoints and crawler-meta endpoints."""

from datetime import datetime

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
from tests.conftest import _TestSession


@pytest.fixture()
def db_with_data():
    db = _TestSession()
    org = Organization(
        id=1, name="Gemeinderat", organization_type="Beschlussgremium",
        scraped_at=datetime(2024, 1, 1),
    )
    person = Person(id=10, name="Max Mustermann", scraped_at=datetime(2024, 1, 1))
    membership = Membership(id=1, person_id=10, organization_id=1, role="Mitglied")
    mtg = Meeting(
        id=100, name="Gemeinderatssitzung", organization_id=1,
        start=datetime(2024, 3, 15, 19, 0),
        detail_scraped_at=datetime(2024, 3, 16),
        scraped_at=datetime(2024, 3, 16),
    )
    paper = Paper(
        id=42, name="Haushalt 2024", reference="VO/24/001",
        paper_type="Beschlussvorlage", scraped_at=datetime(2024, 3, 1),
    )
    ai = AgendaItem(
        id=200, meeting_id=100, paper_id=42, paper_reference="VO/24/001",
        number="Ö 1", name="Haushaltsbeschluss", public=True,
        result="ACCEPTED", resolution_text="Beschlossen.", vote_text="10:0 Stimmen",
    )
    paper_file = File(
        id=1, paper_id=42, name="Haushalt.pdf",
        access_url="https://example.invalid/allris/wicket/resource/doc42.pdf",
        mime_type="application/pdf",
    )
    ai_file = File(
        id=2, agenda_item_id=200, name="Anlage.pdf",
        access_url="https://example.invalid/allris/wicket/resource/doc99.pdf",
        mime_type="application/pdf",
    )
    db.add_all([org, person, membership, mtg, paper, ai, paper_file, ai_file])
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# /md/  — index
# ---------------------------------------------------------------------------

def test_md_index_empty(client):
    r = client.get("/md/")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "Gremien" in r.text


def test_md_index_with_data(client, db_with_data):
    r = client.get("/md/")
    assert r.status_code == 200
    assert "Gemeinderat" in r.text
    assert "md/gremien/1" in r.text
    assert "canonical" in r.headers.get("link", "")


# ---------------------------------------------------------------------------
# /md/gremien/{id}
# ---------------------------------------------------------------------------

def test_md_committee_not_found(client):
    assert client.get("/md/gremien/999").status_code == 404


def test_md_committee_with_data(client, db_with_data):
    r = client.get("/md/gremien/1")
    assert r.status_code == 200
    assert "Gemeinderat" in r.text
    assert "md/sitzungen/100" in r.text
    assert "text/markdown" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /md/sitzungen/
# ---------------------------------------------------------------------------

def test_md_meetings_index_empty(client):
    r = client.get("/md/sitzungen/")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]


def test_md_meetings_index_with_data(client, db_with_data):
    r = client.get("/md/sitzungen/")
    assert r.status_code == 200
    assert "Gemeinderatssitzung" in r.text
    assert "md/sitzungen/100" in r.text


# ---------------------------------------------------------------------------
# /md/sitzungen/{id}
# ---------------------------------------------------------------------------

def test_md_meeting_not_found(client):
    assert client.get("/md/sitzungen/999").status_code == 404


def test_md_meeting_with_data(client, db_with_data):
    r = client.get("/md/sitzungen/100")
    assert r.status_code == 200
    body = r.text
    assert "Gemeinderat" in body
    assert "Haushaltsbeschluss" in body
    assert "angenommen" in body
    assert "Beschlossen." in body
    assert "VO/24/001" in body
    assert "Anlage.pdf" in body
    assert "text/markdown" in r.headers["content-type"]
    assert "Last-Modified" in r.headers


# ---------------------------------------------------------------------------
# /md/vorlagen/{id}
# ---------------------------------------------------------------------------

def test_md_paper_not_found(client):
    assert client.get("/md/vorlagen/999").status_code == 404


def test_md_paper_with_data(client, db_with_data):
    r = client.get("/md/vorlagen/42")
    assert r.status_code == 200
    body = r.text
    assert "Haushalt 2024" in body
    assert "VO/24/001" in body
    assert "Haushalt.pdf" in body
    assert "Beratungen" in body
    assert "Gemeinderatssitzung" in body


# ---------------------------------------------------------------------------
# /md/personen/
# ---------------------------------------------------------------------------

def test_md_persons_empty(client):
    r = client.get("/md/personen/")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]


def test_md_persons_with_data(client, db_with_data):
    r = client.get("/md/personen/")
    assert r.status_code == 200
    assert "Max Mustermann" in r.text
    assert "Gemeinderat" in r.text
    assert "Mitglied" in r.text


# ---------------------------------------------------------------------------
# /robots.txt
# ---------------------------------------------------------------------------

def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Allow: /md/" in r.text
    assert "Allow: /llms.txt" in r.text


# ---------------------------------------------------------------------------
# /llms.txt
# ---------------------------------------------------------------------------

def test_llms_txt_empty(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert "Ratsinformationssystem" in r.text


def test_llms_txt_with_data(client, db_with_data):
    r = client.get("/llms.txt")
    assert r.status_code == 200
    body = r.text
    assert "Gemeinderat" in body
    assert "md/gremien/1" in body
    assert "md/sitzungen/100" in body


# ---------------------------------------------------------------------------
# /sitemap.xml
# ---------------------------------------------------------------------------

def test_sitemap_xml_empty(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.headers["content-type"]
    assert "<urlset" in r.text
    assert "md/" in r.text


def test_sitemap_xml_with_data(client, db_with_data):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    body = r.text
    assert "md/gremien/1" in body
    assert "md/sitzungen/100" in body
    assert "md/vorlagen/42" in body
    assert "<lastmod>" in body
