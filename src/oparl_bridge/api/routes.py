from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from oparl_bridge.config import settings
from oparl_bridge.db.models import AgendaItem, File, Meeting, Organization, Paper
from oparl_bridge.db.session import get_db
from oparl_bridge.normalizer.mapper import OParlMapper
from oparl_bridge.normalizer.oparl_schema import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSystem,
)

router = APIRouter(prefix="/oparl/v1.1")


def get_mapper(request: Request) -> OParlMapper:
    base_url = str(request.base_url).rstrip("/")
    return OParlMapper(settings, base_url=base_url)


@router.get("/", response_model=OParlSystem)
async def get_system(mapper: OParlMapper = Depends(get_mapper)):
    return mapper.system()


@router.get("/bodies", response_model=dict)
async def list_bodies(mapper: OParlMapper = Depends(get_mapper)):
    body = mapper.body()
    return {
        "data": [body.model_dump(by_alias=True, exclude_none=True)],
        "links": {},
        "pagination": {"totalElements": 1, "elementsPerPage": 100, "currentPage": 1},
    }


@router.get("/body/1", response_model=OParlBody)
async def get_body(mapper: OParlMapper = Depends(get_mapper)):
    return mapper.body()


@router.get("/body/1/organizations", response_model=dict)
async def list_organizations(
    mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    orgs = db.query(Organization).all()
    data = [mapper.organization(o).model_dump(by_alias=True, exclude_none=True) for o in orgs]
    return {"data": data, "links": {}, "pagination": {"totalElements": len(data)}}


@router.get("/organization/{org_id}", response_model=OParlOrganization)
async def get_organization(
    org_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return mapper.organization(org)


@router.get("/body/1/meetings", response_model=dict)
async def list_meetings(
    organization: int | None = None,
    mapper: OParlMapper = Depends(get_mapper),
    db: Session = Depends(get_db),
):
    q = db.query(Meeting)
    if organization is not None:
        q = q.filter(Meeting.organization_id == organization)
    meetings = q.all()
    data = [mapper.meeting(m).model_dump(by_alias=True, exclude_none=True) for m in meetings]
    return {"data": data, "links": {}, "pagination": {"totalElements": len(data)}}


@router.get("/meeting/{meeting_id}", response_model=OParlMeeting)
async def get_meeting(
    meeting_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    mtg = db.get(Meeting, meeting_id)
    if mtg is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return mapper.meeting(mtg)


@router.get("/agendaitem/{item_id}", response_model=OParlAgendaItem)
async def get_agenda_item(
    item_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    item = db.get(AgendaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AgendaItem not found")
    return mapper.agenda_item(item)


@router.get("/body/1/papers", response_model=dict)
async def list_papers(
    mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    papers = db.query(Paper).all()
    data = [mapper.paper(p).model_dump(by_alias=True, exclude_none=True) for p in papers]
    return {"data": data, "links": {}, "pagination": {"totalElements": len(data)}}


@router.get("/paper/{paper_id}", response_model=OParlPaper)
async def get_paper(
    paper_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    p = db.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return mapper.paper(p)


@router.get("/file/{file_id}", response_model=OParlFile)
async def get_file(
    file_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    f = db.get(File, file_id)
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")
    return mapper.file(f)
