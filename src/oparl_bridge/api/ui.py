"""UI-optimised endpoints — denormalised data for the SPA, not OParl-compliant."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from oparl_bridge.db.models import File, Meeting, Organization
from oparl_bridge.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui")


def _top_sort_key(number: str | None) -> tuple[int, int]:
    """Numeric sort key for TOP numbers like 'Ö 4', 'Ö 4.1', 'N 2'."""
    if not number:
        return (999999, 0)
    m = re.search(r"(\d+)(?:\.(\d+))?", number)
    if not m:
        return (999999, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


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
                "files": [
                    {"id": f.id, "name": f.name, "url": f.access_url}
                    for f in ai.paper.files
                ],
            }
        items.append({
            "id": ai.id,
            "number": ai.number,
            "name": ai.name,
            "public": ai.public,
            "result": ai.result,
            "resolutionText": ai.resolution_text,
            "voteText": ai.vote_text,
            "paper": paper,
            "files": [{"id": f.id, "name": f.name} for f in ai.files],
        })

    return {
        "id": mtg.id,
        "name": mtg.name,
        "start": mtg.start.isoformat() if mtg.start else None,
        "location": mtg.location,
        "items": items,
    }


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

    if f.paper_id is not None:
        source_id = f.paper_id
        source_page = "paper"
    elif f.agenda_item_id is not None:
        ai = db.get(AgendaItem, f.agenda_item_id)
        if ai is None or ai.meeting_id is None:
            raise HTTPException(status_code=422, detail="File has no resolvable source page")
        source_id = ai.meeting_id
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
            detail=f"Could not fetch PDF ({f.access_url}): {exc}",
        )

    if content is None:
        raise HTTPException(status_code=502, detail="ALLRIS returned no content for this file")

    return Response(content, media_type="application/pdf")
