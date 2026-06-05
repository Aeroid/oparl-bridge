"""UI-optimised endpoints — denormalised data for the SPA, not OParl-compliant."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from oparl_bridge.db.models import File, Meeting, Organization
from oparl_bridge.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui")


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
            for ai in mtg.agenda_items
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
    for ai in mtg.agenda_items:
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
            "paper": paper,
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

    if f.paper_id is None:
        raise HTTPException(status_code=422, detail="File has no associated paper")

    from oparl_bridge.scraper import AllrisScraper
    try:
        async with AllrisScraper().session() as scraper:
            content = await scraper.fetch_file_content(f.paper_id, f.access_url)
    except Exception as exc:
        logger.warning("Playwright PDF fetch failed for file %d: %s", file_id, exc)
        raise HTTPException(status_code=502, detail=f"Could not fetch PDF: {exc}")

    if content is None:
        raise HTTPException(status_code=502, detail="ALLRIS returned no content for this file")

    return Response(content, media_type="application/pdf")
