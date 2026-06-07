"""Markdown endpoints — LLM-crawler-friendly plain-text views of OParl data."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from oparl_bridge.config import settings
from oparl_bridge.db.models import AgendaItem, Meeting, Membership, Organization, Paper, Person
from oparl_bridge.db.session import get_db
from oparl_bridge.wikidata import get_wikidata

router = APIRouter()

_DE_MONTHS = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "unbekannt"
    return f"{dt.day}. {_DE_MONTHS[dt.month]} {dt.year}, {dt.strftime('%H:%M')} Uhr"


def _fmt_date_short(dt: datetime | None) -> str:
    if dt is None:
        return "unbekannt"
    return f"{dt.day}. {_DE_MONTHS[dt.month]} {dt.year}"


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _http_date(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _md_response(content: str, canonical: str, last_modified: datetime | None = None) -> PlainTextResponse:
    headers: dict[str, str] = {
        "Link": f'<{canonical}>; rel="canonical"',
    }
    if last_modified:
        headers["Last-Modified"] = _http_date(last_modified)
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8", headers=headers)


def _footer() -> str:
    allris = settings.allris_base_url.rstrip("/")
    return (
        "\n---\n"
        f"Daten abgerufen von oparl-bridge (https://github.com/Aeroid/oparl-bridge)  \n"
        f"Originaldaten: © {settings.body_name} — öffentlich zugängliches Ratsinformationssystem ([Originalquelle]({allris}/))  \n"
        f"Normdaten: [Wikidata](https://www.wikidata.org/) (CC0)\n"
    )


# ---------------------------------------------------------------------------
# GET /md/  — index of all committees
# ---------------------------------------------------------------------------

@router.get("/md/", response_class=PlainTextResponse)
async def md_index(request: Request, db: Session = Depends(get_db)):
    base = _base(request)
    orgs = db.query(Organization).order_by(Organization.name).all()
    latest_scraped = max((o.scraped_at for o in orgs if o.scraped_at), default=None)
    wd = await get_wikidata(settings.wikidata_id) if settings.wikidata_id else None

    lines = [f"# {settings.body_name} — Ratsinformationssystem\n"]

    if wd:
        if wd.bundesland or wd.landkreis:
            parts = []
            if wd.landkreis:
                lurl = f"https://www.wikidata.org/wiki/{wd.landkreis_qid}" if wd.landkreis_qid else None
                parts.append(f"[{wd.landkreis}]({lurl})" if lurl else wd.landkreis)
            if wd.bundesland:
                burl = f"https://www.wikidata.org/wiki/{wd.bundesland_qid}" if wd.bundesland_qid else None
                parts.append(f"[{wd.bundesland}]({burl})" if burl else wd.bundesland)
            lines.append("Lage: " + " · ".join(parts) + "  ")
        if wd.population:
            lines.append(f"Einwohner: {wd.population:,}  ".replace(",", "."))
        if wd.area:
            lines.append(f"Fläche: {wd.area:,.2f} km²  ".replace(",", "."))
        if wd.mayor:
            lines.append(f"Bürgermeister/in: {wd.mayor}  ")
        if wd.ags:
            lines.append(f"AGS: {wd.ags}  ")
        if wd.lat and wd.lon:
            lines.append(f"Koordinaten: {wd.lat}°N, {wd.lon}°E  ")
        lines.append(f"Wikidata: [{wd.qid}]({wd.wikidata_url})  ")
        if wd.wikipedia_de:
            lines.append(f"Wikipedia: {wd.wikipedia_de}  ")
        if wd.gnd:
            lines.append(f"GND: [{wd.gnd}](https://d-nb.info/gnd/{wd.gnd})  ")
        if wd.osm_id:
            lines.append(f"OpenStreetMap: [Relation {wd.osm_id}](https://www.openstreetmap.org/relation/{wd.osm_id})  ")
        lines.append("")

    lines.append("## Gremien\n")
    for org in orgs:
        count = db.query(Meeting).filter(Meeting.organization_id == org.id).count()
        if count == 0:
            continue
        lines.append(f"- [{org.name}]({base}/md/gremien/{org.id}) ({count} Sitzungen)")

    lines.append("")
    lines.append(f"[Alle Sitzungen chronologisch]({base}/md/sitzungen/)")
    lines.append(f"[Personen und Mitgliedschaften]({base}/md/personen/)")
    lines.append(_footer())
    return _md_response("\n".join(lines), canonical=f"{base}/md/", last_modified=latest_scraped)


# ---------------------------------------------------------------------------
# GET /md/gremien/{id}  — committee overview
# ---------------------------------------------------------------------------

@router.get("/md/gremien/{org_id}", response_class=PlainTextResponse)
async def md_committee(org_id: int, request: Request, db: Session = Depends(get_db)):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404)

    base = _base(request)
    meetings = (
        db.query(Meeting)
        .filter(Meeting.organization_id == org_id)
        .order_by(Meeting.start.desc())
        .all()
    )

    lines = [f"# {org.name}\n"]
    if org.organization_type:
        lines.append(f"Typ: {org.organization_type}  ")
    lines.append(f"Sitzungen: {len(meetings)}  ")
    lines.append(f"[← Alle Gremien]({base}/md/)\n")
    lines.append("## Sitzungen\n")

    # Merge real meetings with planned (no-ID) entries, sort descending
    import json as _json
    from datetime import datetime as _dt
    future_dates = _json.loads(org.future_meeting_dates) if org.future_meeting_dates else []
    if not future_dates and org.next_meeting_date:
        future_dates = [org.next_meeting_date]
    existing_starts = {m.start.isoformat()[:16] for m in meetings if m.start}

    # Build unified list: (iso_str, line_text)
    entries: list[tuple[str, str]] = []
    for mtg in meetings:
        iso = mtg.start.isoformat() if mtg.start else ""
        entries.append((iso, f"- [{_fmt_date_short(mtg.start)} — {mtg.name}]({base}/md/sitzungen/{mtg.id})"))
    for iso in future_dates:
        if iso[:16] not in existing_starts:
            try:
                entries.append((iso, f"- {_fmt_date_short(_dt.fromisoformat(iso))} — {org.name} (geplant)"))
            except ValueError:
                pass

    for _, line in sorted(entries, key=lambda x: x[0], reverse=True):
        lines.append(line)

    lines.append(_footer())
    latest = max((m.scraped_at for m in meetings if m.scraped_at), default=org.scraped_at)
    return _md_response("\n".join(lines), canonical=f"{base}/md/gremien/{org_id}", last_modified=latest)


# ---------------------------------------------------------------------------
# GET /md/sitzungen/  — all meetings (recent first)
# ---------------------------------------------------------------------------

@router.get("/md/sitzungen/", response_class=PlainTextResponse)
async def md_meetings_index(request: Request, db: Session = Depends(get_db)):
    base = _base(request)
    meetings = db.query(Meeting).order_by(Meeting.start.desc()).limit(200).all()
    orgs = {o.id: o.name for o in db.query(Organization).all()}
    latest = max((m.scraped_at for m in meetings if m.scraped_at), default=None)

    lines = [f"# {settings.body_name} — Alle Sitzungen\n"]
    for mtg in meetings:
        org_name = orgs.get(mtg.organization_id, "")
        date_str = _fmt_date_short(mtg.start)
        lines.append(f"- [{date_str} — {mtg.name}]({base}/md/sitzungen/{mtg.id})")
        if org_name:
            lines.append(f"  Gremium: {org_name}")

    lines.append(_footer())
    return _md_response("\n".join(lines), canonical=f"{base}/md/sitzungen/", last_modified=latest)


# ---------------------------------------------------------------------------
# GET /md/sitzungen/{id}  — full meeting page
# ---------------------------------------------------------------------------

@router.get("/md/sitzungen/{meeting_id}", response_class=PlainTextResponse)
async def md_meeting(meeting_id: int, request: Request, db: Session = Depends(get_db)):
    mtg = db.get(Meeting, meeting_id)
    if mtg is None:
        raise HTTPException(status_code=404)

    base = _base(request)
    org = db.get(Organization, mtg.organization_id) if mtg.organization_id else None
    allris_base = settings.allris_base_url.rstrip("/")

    lines = [f"# {org.name if org else ''} — {_fmt_date_short(mtg.start)}\n"]
    lines.append(f"Datum: {_fmt_date(mtg.start)}  ")
    if mtg.location:
        lines.append(f"Ort: {mtg.location}  ")
    if org:
        lines.append(f"Gremium: [{org.name}]({base}/md/gremien/{org.id})  ")
    lines.append(f"[Originalquelle]({allris_base}/to010?SILFDNR={mtg.id})  ")
    lines.append("")

    if mtg.files:
        lines.append("## Sitzungsdokumente\n")
        for f in mtg.files:
            lines.append(f"- [{f.name}]({f.access_url})")
        lines.append("")

    from oparl_bridge.api.ui import _top_sort_key
    items = sorted(mtg.agenda_items, key=lambda ai: _top_sort_key(ai.number))

    if items:
        lines.append("## Tagesordnung\n")
        for ai in items:
            num = ai.number or "—"
            public = "" if ai.public else " *(nichtöffentlich)*"
            lines.append(f"### {num}. {ai.name}{public}\n")

            if ai.paper:
                ref = ai.paper.reference or ai.paper_reference or ""
                lines.append(f"Vorlage: [{ref}]({base}/md/vorlagen/{ai.paper.id})  ")
                for f in ai.paper.files:
                    lines.append(f"PDF: [{f.name}]({f.access_url})  ")
            elif ai.paper_reference:
                lines.append(f"Vorlage: {ai.paper_reference}  ")

            if ai.result:
                result_labels = {
                    "ACCEPTED": "angenommen", "REJECTED": "abgelehnt",
                    "DEFERRED": "vertagt", "NODECISION": "keine Abstimmung",
                }
                lines.append(f"Beschluss: {result_labels.get(ai.result, ai.result)}  ")
            if ai.resolution_text:
                lines.append(f"\n{ai.resolution_text}\n")
            if ai.vote_text:
                lines.append(f"\n{ai.vote_text}\n")
            if ai.word_contribution:
                lines.append(f"\n{ai.word_contribution}\n")

            for f in ai.files:
                if f.access_url.startswith("allris://to020/"):
                    tolfdnr = f.access_url.split("/")[2]
                    url = f"{allris_base}/to020?TOLFDNR={tolfdnr}"
                else:
                    url = f.access_url
                lines.append(f"Anlage: [{f.name}]({url})  ")

            lines.append("")

    lines.append(_footer())
    last_mod = mtg.detail_scraped_at or mtg.scraped_at
    return _md_response("\n".join(lines), canonical=f"{base}/md/sitzungen/{meeting_id}", last_modified=last_mod)


# ---------------------------------------------------------------------------
# GET /md/vorlagen/{id}  — paper/Drucksache
# ---------------------------------------------------------------------------

@router.get("/md/vorlagen/{paper_id}", response_class=PlainTextResponse)
async def md_paper(paper_id: int, request: Request, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404)

    base = _base(request)
    allris_base = settings.allris_base_url.rstrip("/")

    lines = [f"# {paper.name}\n"]
    if paper.reference:
        lines.append(f"Aktenzeichen: {paper.reference}  ")
    if paper.paper_type:
        lines.append(f"Art: {paper.paper_type}  ")
    lines.append(f"[Originalquelle]({allris_base}/vo020?VOLFDNR={paper.id})  ")
    lines.append("")

    if paper.files:
        lines.append("## Dokumente\n")
        for f in paper.files:
            lines.append(f"- [{f.name}]({f.access_url})")
        lines.append("")

    consultations = (
        db.query(AgendaItem)
        .filter(AgendaItem.paper_id == paper.id)
        .all()
    )
    if consultations:
        lines.append("## Beratungen\n")
        for ai in consultations:
            if ai.meeting_id:
                mtg = db.get(Meeting, ai.meeting_id)
                if mtg:
                    date_str = _fmt_date_short(mtg.start)
                    lines.append(
                        f"- [{date_str} — {mtg.name}]({base}/md/sitzungen/{mtg.id})"
                        f" — TOP {ai.number or '?'}: {ai.name}"
                    )
        lines.append("")

    lines.append(_footer())
    return _md_response("\n".join(lines), canonical=f"{base}/md/vorlagen/{paper_id}", last_modified=paper.scraped_at)


# ---------------------------------------------------------------------------
# GET /md/personen/  — all persons with memberships
# ---------------------------------------------------------------------------

@router.get("/md/personen/", response_class=PlainTextResponse)
async def md_persons(request: Request, db: Session = Depends(get_db)):
    base = _base(request)
    persons = db.query(Person).order_by(Person.name).all()
    orgs = {o.id: o for o in db.query(Organization).all()}
    memberships = db.query(Membership).all()
    latest = max((p.scraped_at for p in persons if p.scraped_at), default=None)

    person_memberships: dict[int, list[Membership]] = {}
    for m in memberships:
        person_memberships.setdefault(m.person_id, []).append(m)

    lines = [f"# {settings.body_name} — Personen und Mitgliedschaften\n"]
    lines.append(f"{len(persons)} Personen · [← Alle Gremien]({base}/md/)\n")

    for person in persons:
        lines.append(f"## {person.name}\n")
        ms = person_memberships.get(person.id, [])
        if ms:
            for m in ms:
                org = orgs.get(m.organization_id)
                if org:
                    org_link = f"[{org.name}]({base}/md/gremien/{org.id})"
                    role_str = f", {m.role}" if m.role else ""
                    lines.append(f"- {org_link}{role_str}")
        else:
            lines.append("- *(keine Mitgliedschaft erfasst)*")
        lines.append("")

    lines.append(_footer())
    return _md_response("\n".join(lines), canonical=f"{base}/md/personen/", last_modified=latest)
