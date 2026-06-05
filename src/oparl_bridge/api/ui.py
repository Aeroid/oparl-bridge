"""UI-optimised endpoints — denormalised data for the SPA, not OParl-compliant."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from oparl_bridge.db.models import Meeting
from oparl_bridge.db.session import get_db

router = APIRouter(prefix="/ui")


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
                    {"name": f.name, "url": f.access_url}
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
