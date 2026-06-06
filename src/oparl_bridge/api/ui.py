"""UI-optimised endpoints — denormalised data for the SPA, not OParl-compliant."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from oparl_bridge.db.models import File, Meeting, Organization, Person
from oparl_bridge.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui")


def _top_sort_key(number: str | None) -> tuple[int, int, int]:
    """Sort key for TOP numbers: Ö (öffentlich) first, then N (nichtöffentlich), numeric within each."""
    if not number:
        return (2, 999999, 0)
    upper = number.strip().upper()
    if upper.startswith("Ö"):
        group = 0
    elif upper.startswith("N"):
        group = 1
    else:
        group = 2
    m = re.search(r"(\d+)(?:\.(\d+))?", number)
    if not m:
        return (group, 999999, 0)
    return (group, int(m.group(1)), int(m.group(2) or 0))


@router.get("/all")
async def ui_all(db: Session = Depends(get_db)):
    """Return all meetings with inlined org name and agenda items for client-side search."""
    orgs = {o.id: o.name for o in db.query(Organization).all()}
    result = []
    for mtg in db.query(Meeting).order_by(Meeting.start.desc()).all():
        items = [
            {
                "id": ai.id,
                "number": ai.number,
                "name": ai.name,
                "public": ai.public,
                "paperRef": ai.paper_reference,
            }
            for ai in sorted(mtg.agenda_items, key=lambda ai: _top_sort_key(ai.number))
        ]
        result.append({
            "id": mtg.id,
            "name": mtg.name,
            "start": mtg.start.isoformat() if mtg.start else None,
            "location": mtg.location,
            "orgId": mtg.organization_id,
            "orgName": orgs.get(mtg.organization_id, ""),
            "items": items,
        })
    return {"meetings": result}


def _display_url(access_url: str) -> str:
    """Convert internal sentinel URLs to human-readable ALLRIS source URLs."""
    from oparl_bridge.config import settings
    base = settings.allris_base_url.rstrip("/")
    if access_url.startswith("allris://to020/"):
        tolfdnr = access_url.replace("allris://to020/", "").split("/")[0]
        return f"{base}/to020?TOLFDNR={tolfdnr}"
    return access_url


def _file_dict(f) -> dict:
    url = _display_url(f.access_url)
    is_sentinel = f.access_url.startswith("allris://to020/")
    return {
        "id": f.id,
        "name": f.name,
        "url": url,
        "displayLabel": f.name if is_sentinel else None,
    }


@router.get("/meeting/{meeting_id}")
async def ui_meeting(meeting_id: int, db: Session = Depends(get_db)):
    """Return a meeting with agenda items, papers, and file URLs inlined."""
    mtg = db.get(Meeting, meeting_id)
    if mtg is None:
        raise HTTPException(status_code=404, detail="Meeting not found")

    items = []
    for ai in sorted(mtg.agenda_items, key=lambda ai: _top_sort_key(ai.number)):
        paper = None
        if ai.paper:
            paper = {
                "id": ai.paper.id,
                "name": ai.paper.name,
                "reference": ai.paper.reference or ai.paper_reference,
                "type": ai.paper.paper_type,
                "files": [_file_dict(f) for f in ai.paper.files],
            }
        items.append({
            "id": ai.id,
            "number": ai.number,
            "name": ai.name,
            "public": ai.public,
            "result": ai.result,
            "resolutionText": ai.resolution_text,
            "voteText": ai.vote_text,
            "wordContribution": ai.word_contribution,
            "paper": paper,
            "files": [_file_dict(f) for f in ai.files],
        })

    meeting_files = [_file_dict(f) for f in mtg.files]

    return {
        "id": mtg.id,
        "name": mtg.name,
        "start": mtg.start.isoformat() if mtg.start else None,
        "location": mtg.location,
        "files": meeting_files,
        "items": items,
    }


@router.get("/orgs")
async def ui_orgs(db: Session = Depends(get_db)):
    """All organizations with type — for the org list incl. orgs without meetings."""
    orgs = db.query(Organization).order_by(Organization.name).all()
    return {
        "orgs": [
            {"id": o.id, "name": o.name, "type": o.organization_type}
            for o in orgs
        ]
    }


@router.get("/persons")
async def ui_persons(db: Session = Depends(get_db)):
    """All persons with their org memberships — used for the persons overview."""
    persons = db.query(Person).order_by(Person.name).all()
    return {
        "persons": [
            {
                "id": p.id,
                "name": p.name,
                "memberships": [
                    {
                        "orgId": m.organization_id,
                        "orgName": m.organization.name,
                        "orgType": m.organization.organization_type,
                        "role": m.role,
                    }
                    for m in p.memberships
                    if m.organization is not None
                ],
            }
            for p in persons
        ]
    }


@router.get("/org/{org_id}/members")
async def ui_org_members(org_id: int, db: Session = Depends(get_db)):
    """Members of a specific organization."""
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    def _fraction(person):
        for pm in person.memberships:
            if pm.organization and pm.organization.organization_type == "Fraktion/Gruppe":
                return pm.organization.name
        return None

    members = sorted(
        [
            {
                "personId": m.person_id,
                "name": m.person.name,
                "role": m.role,
                "fraction": _fraction(m.person),
            }
            for m in org.memberships
        ],
        key=lambda x: x["name"],
    )
    return {"members": members}


_municipality_cache: dict | None = None


@router.get("/municipality")
async def ui_municipality():
    """Fetch municipality metadata from Wikidata (cached after first call)."""
    from oparl_bridge.config import settings

    global _municipality_cache
    if _municipality_cache is not None:
        return _municipality_cache
    if not settings.wikidata_id:
        _municipality_cache = {"allrisBaseUrl": settings.allris_base_url.rstrip("/")}
        return _municipality_cache

    import urllib.parse

    import httpx

    qid = settings.wikidata_id
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "oparl-bridge/1.0"})
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("Wikidata fetch failed for %s: %s", qid, exc)
        _municipality_cache = {}
        return _municipality_cache

    entity = data.get("entities", {}).get(qid, {})
    claims = entity.get("claims", {})

    # Coat of arms (P94), logo (P154), flag (P41), image (P18)
    wappen_url = None
    for prop in ["P94", "P154", "P41", "P18"]:
        if prop in claims:
            try:
                filename = claims[prop][0]["mainsnak"]["datavalue"]["value"]
                filename = filename.replace(" ", "_")
                encoded = urllib.parse.quote(filename, safe="")
                wappen_url = (
                    f"https://commons.wikimedia.org/wiki/Special:FilePath/{encoded}?width=120"
                )
                break
            except (KeyError, TypeError):
                continue

    # German Wikipedia link
    wikipedia_url = None
    sitelinks = entity.get("sitelinks", {})
    for lang in ["dewiki", "enwiki"]:
        if lang in sitelinks:
            title = sitelinks[lang]["title"].replace(" ", "_")
            wiki = "de" if lang == "dewiki" else "en"
            wikipedia_url = f"https://{wiki}.wikipedia.org/wiki/{urllib.parse.quote(title)}"
            break

    # Population — most recent P1082 value
    population = None
    if "P1082" in claims:
        try:
            pop_claims = [c for c in claims["P1082"] if "datavalue" in c.get("mainsnak", {})]
            pop_claims.sort(
                key=lambda c: (
                    c.get("qualifiers", {})
                    .get("P585", [{}])[0]
                    .get("datavalue", {})
                    .get("value", {})
                    .get("time", "")
                ),
                reverse=True,
            )
            if pop_claims:
                amount = pop_claims[0]["mainsnak"]["datavalue"]["value"]["amount"]
                population = int(float(amount))
        except (KeyError, TypeError, ValueError, IndexError):
            pass

    _municipality_cache = {
        "wikidataId": qid,
        "wikidataUrl": f"https://www.wikidata.org/wiki/{qid}",
        "wappenUrl": wappen_url,
        "wikipediaUrl": wikipedia_url,
        "population": population,
        "allrisBaseUrl": settings.allris_base_url.rstrip("/"),
    }
    return _municipality_cache


@router.get("/proxy/file/{file_id}")
async def proxy_file(file_id: int, db: Session = Depends(get_db)):
    """Fetch a PDF from ALLRIS using Playwright route interception.

    Wicket resource URLs are session-scoped. We navigate to the vo020 page for
    the paper, register a route interceptor for the PDF URL, click the link,
    and capture the bytes — the only approach that returns a 200 response.
    """
    f = db.get(File, file_id)
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")

    from oparl_bridge.db.models import AgendaItem
    from oparl_bridge.scraper import AllrisScraper

    if f.access_url.startswith("allris://to020/"):
        # Sentinel URL for to020 Anlagen: allris://to020/{tolfdnr}/{index}
        parts = f.access_url.replace("allris://to020/", "").split("/")
        source_id = int(parts[0])
        source_page = "to020-anlage"
    elif f.paper_id is not None:
        source_id = f.paper_id
        source_page = "paper"
    elif f.agenda_item_id is not None:
        ai = db.get(AgendaItem, f.agenda_item_id)
        if ai is None or ai.meeting_id is None:
            raise HTTPException(status_code=422, detail="File has no resolvable source page")
        source_id = ai.meeting_id
        source_page = "meeting"
    elif f.meeting_id is not None:
        source_id = f.meeting_id
        source_page = "meeting"
    else:
        raise HTTPException(status_code=422, detail="File has no associated paper or agenda item")

    try:
        async with AllrisScraper().session() as scraper:
            content = await scraper.fetch_file_content(source_id, f.access_url, source_page)
    except Exception as exc:
        logger.warning("PDF fetch failed id=%d url=%s err=%s", file_id, f.access_url, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch PDF  {f.access_url}  {exc}",
        )

    if content is None:
        raise HTTPException(status_code=502, detail="ALLRIS returned no content for this file")

    return Response(content, media_type="application/pdf")
