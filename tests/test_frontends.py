"""Tests for create_app() frontend selection flags.

Each test creates a tailored app instance directly (bypassing the conftest
`app` fixture) so it can verify route presence/absence per configuration.
"""

from fastapi.testclient import TestClient

from oparl_bridge.db.session import get_db
from oparl_bridge.main import create_app
from tests.conftest import _override_get_db


def _client(**flags) -> TestClient:
    a = create_app(**flags)
    a.dependency_overrides[get_db] = _override_get_db
    return TestClient(a)


# ---------------------------------------------------------------------------
# All-enabled (sanity check)
# ---------------------------------------------------------------------------

def test_all_enabled_by_default():
    c = _client()
    assert c.get("/oparl/v1.1/").status_code == 200
    assert c.get("/md/").status_code == 200
    assert c.get("/robots.txt").status_code == 200
    assert c.get("/").status_code == 200


# ---------------------------------------------------------------------------
# OParl disabled
# ---------------------------------------------------------------------------

def test_no_oparl_disables_oparl_routes():
    c = _client(enable_oparl=False)
    assert c.get("/oparl/v1.1/").status_code == 404
    assert c.get("/oparl/v1.1/body/1").status_code == 404
    assert c.get("/oparl/v1.1/body/1/organizations").status_code == 404


def test_no_oparl_keeps_other_frontends():
    c = _client(enable_oparl=False)
    assert c.get("/md/").status_code == 200
    assert c.get("/").status_code == 200


# ---------------------------------------------------------------------------
# MD / crawler disabled
# ---------------------------------------------------------------------------

def test_no_md_disables_md_routes():
    c = _client(enable_md=False)
    assert c.get("/md/").status_code == 404
    assert c.get("/md/sitzungen/").status_code == 404
    assert c.get("/robots.txt").status_code == 404
    assert c.get("/llms.txt").status_code == 404
    assert c.get("/sitemap.xml").status_code == 404


def test_no_md_keeps_other_frontends():
    c = _client(enable_md=False)
    assert c.get("/oparl/v1.1/").status_code == 200
    assert c.get("/").status_code == 200


# ---------------------------------------------------------------------------
# SPA disabled
# ---------------------------------------------------------------------------

def test_no_spa_disables_spa_routes():
    c = _client(enable_spa=False)
    assert c.get("/ui/all").status_code == 404
    assert c.get("/ui/meeting/1").status_code == 404


def test_no_spa_root_shows_landing_page():
    c = _client(enable_spa=False)
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "OParl" in r.text
    assert "Markdown" in r.text


def test_no_spa_landing_page_omits_disabled_frontends():
    c = _client(enable_spa=False, enable_md=False)
    r = c.get("/")
    assert r.status_code == 200
    assert "OParl" in r.text
    assert "Markdown" not in r.text


def test_no_spa_keeps_other_frontends():
    c = _client(enable_spa=False)
    assert c.get("/oparl/v1.1/").status_code == 200
    assert c.get("/md/").status_code == 200


# ---------------------------------------------------------------------------
# Favicon is always served
# ---------------------------------------------------------------------------

def test_index_html_redirects_to_root():
    for flags in [{}, {"enable_spa": False}]:
        c = _client(**flags)
        r = c.get("/index.html", follow_redirects=False)
        assert r.status_code == 301, f"expected 301 with flags={flags}"
        assert r.headers["location"] == "/"


def test_favicon_always_present():
    for flags in [
        {},
        {"enable_oparl": False},
        {"enable_md": False},
        {"enable_spa": False},
    ]:
        c = _client(**flags)
        assert c.get("/favicon.ico").status_code == 200, f"favicon missing with flags={flags}"


# ---------------------------------------------------------------------------
# All frontends disabled → startup error
# ---------------------------------------------------------------------------

def test_all_disabled_raises():
    import pytest
    with pytest.raises(ValueError, match="At least one frontend"):
        create_app(enable_oparl=False, enable_md=False, enable_spa=False)
