"""UI-optimised endpoints — denormalised data for the SPA, not OParl-compliant."""

import json
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from oparl_bridge.db.models import File, Meeting, Organization
from oparl_bridge.db.session import get_db

router = APIRouter(prefix="/ui")

_COOKIES_PATH = Path("oparl_cookies.json")


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
    """Proxy a PDF from ALLRIS using stored session cookies."""
    f = db.get(File, file_id)
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")

    if not _COOKIES_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="No session cookies available — run oparl-bridge-sync first",
        )

    raw = json.loads(_COOKIES_PATH.read_text())
    cookies = {c["name"]: c["value"] for c in raw}

    async def stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", f.access_url, cookies=cookies) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail="ALLRIS fetch failed")
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    return StreamingResponse(stream(), media_type="application/pdf")
