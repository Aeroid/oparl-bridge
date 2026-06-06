from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from oparl_bridge.config import settings
from oparl_bridge.db.models import AgendaItem, File, Meeting, Membership, Organization, Paper, Person
from oparl_bridge.db.session import get_db
from oparl_bridge.normalizer.mapper import OParlMapper
from oparl_bridge.wikidata import WikidataData, get_wikidata
from oparl_bridge.normalizer.oparl_schema import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSystem,
)

router = APIRouter(prefix="/oparl/v1.1")


def _md_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _with_md(d: dict, base_url: str, path: str) -> dict:
    d["x-markdownUrl"] = _md_url(base_url, path)
    return d


def get_mapper(request: Request) -> OParlMapper:
    base_url = str(request.base_url).rstrip("/")
    return OParlMapper(settings, base_url=base_url)


async def get_wikidata_dep() -> WikidataData | None:
    if not settings.wikidata_id:
        return None
    return await get_wikidata(settings.wikidata_id)


@router.get("/", response_model=OParlSystem)
async def get_system(mapper: OParlMapper = Depends(get_mapper)):
    return Response(
        content=mapper.system().model_dump_json(by_alias=True, exclude_none=True),
        media_type="application/json",
    )


@router.get("/bodies", response_model=dict)
async def list_bodies(
    mapper: OParlMapper = Depends(get_mapper),
    wd: WikidataData | None = Depends(get_wikidata_dep),
):
    body = mapper.body(wikidata=wd)
    import json
    return Response(
        content=json.dumps({
            "data": [body.model_dump(by_alias=True, exclude_none=True)],
            "links": {},
            "pagination": {"totalElements": 1, "elementsPerPage": 100, "currentPage": 1},
        }),
        media_type="application/json",
    )


@router.get("/body/1", response_model=OParlBody)
async def get_body(
    mapper: OParlMapper = Depends(get_mapper),
    wd: WikidataData | None = Depends(get_wikidata_dep),
):
    import json
    body = mapper.body(wikidata=wd)
    return Response(
        content=json.dumps(body.model_dump(by_alias=True, exclude_none=True)),
        media_type="application/json",
    )


@router.get("/body/1/organizations", response_model=dict)
async def list_organizations(
    mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    orgs = db.query(Organization).all()
    data = [_with_md(mapper.organization(o).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/gremien/{o.id}") for o in orgs]
    import json
    return Response(
        content=json.dumps({"data": data, "links": {}, "pagination": {"totalElements": len(data)}}),
        media_type="application/json",
        headers={"Link": f'<{mapper._base_url}/md/>; rel="alternate"; type="text/markdown"'},
    )


@router.get("/organization/{org_id}")
async def get_organization(
    org_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    import json
    d = _with_md(mapper.organization(org).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/gremien/{org_id}")
    return Response(
        content=json.dumps(d),
        media_type="application/json",
        headers={"Link": f'<{mapper._base_url}/md/gremien/{org_id}>; rel="alternate"; type="text/markdown"'},
    )


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
    data = [_with_md(mapper.meeting(m).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/sitzungen/{m.id}") for m in meetings]
    import json
    return Response(
        content=json.dumps({"data": data, "links": {}, "pagination": {"totalElements": len(data)}}, default=str),
        media_type="application/json",
        headers={"Link": f'<{mapper._base_url}/md/sitzungen/>; rel="alternate"; type="text/markdown"'},
    )


@router.get("/meeting/{meeting_id}")
async def get_meeting(
    meeting_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    mtg = db.get(Meeting, meeting_id)
    if mtg is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    import json
    d = _with_md(mapper.meeting(mtg).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/sitzungen/{meeting_id}")
    return Response(
        content=json.dumps(d, default=str),
        media_type="application/json",
        headers={"Link": f'<{mapper._base_url}/md/sitzungen/{meeting_id}>; rel="alternate"; type="text/markdown"'},
    )


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
    data = [_with_md(mapper.paper(p).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/vorlagen/{p.id}") for p in papers]
    return {"data": data, "links": {}, "pagination": {"totalElements": len(data)}}


@router.get("/paper/{paper_id}")
async def get_paper(
    paper_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    p = db.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    import json
    d = _with_md(mapper.paper(p).model_dump(by_alias=True, exclude_none=True), mapper._base_url, f"/md/vorlagen/{paper_id}")
    return Response(
        content=json.dumps(d, default=str),
        media_type="application/json",
        headers={"Link": f'<{mapper._base_url}/md/vorlagen/{paper_id}>; rel="alternate"; type="text/markdown"'},
    )


@router.get("/file/{file_id}", response_model=OParlFile)
async def get_file(
    file_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    f = db.get(File, file_id)
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")
    return mapper.file(f)


@router.get("/body/1/persons", response_model=dict)
async def list_persons(
    mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    persons = db.query(Person).all()
    data = [mapper.person(p).model_dump(by_alias=True, exclude_none=True) for p in persons]
    return {"data": data, "links": {}, "pagination": {"totalElements": len(data)}}


@router.get("/person/{person_id}", response_model=OParlPerson)
async def get_person(
    person_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    p = db.get(Person, person_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return mapper.person(p)


@router.get("/membership/{membership_id}", response_model=OParlMembership)
async def get_membership(
    membership_id: int, mapper: OParlMapper = Depends(get_mapper), db: Session = Depends(get_db)
):
    m = db.get(Membership, membership_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    return mapper.membership(m)
