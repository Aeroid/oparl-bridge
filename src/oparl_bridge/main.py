"""oparl-bridge: OParl 1.1 gateway for ALLRIS municipal information systems."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from oparl_bridge.api.md import router as md_router
from oparl_bridge.api.routes import router
from oparl_bridge.api.ui import router as ui_router
from oparl_bridge.config import settings
from oparl_bridge.db.session import get_db, init_db

_STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("oparl-bridge started. ALLRIS base: %s", settings.allris_base_url)
    if settings.wikidata_id:
        import asyncio
        from oparl_bridge.wikidata import warm_cache
        asyncio.ensure_future(warm_cache(settings.wikidata_id))
    yield


app = FastAPI(
    title="oparl-bridge",
    description=(
        "OParl 1.1-compatible API gateway for ALLRIS municipal information systems. "
        "Exposes data from Apache Wicket-based ALLRIS instances as standard OParl REST API."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(ui_router)
app.include_router(md_router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    if settings.favicon_b64 and settings.favicon_b64.startswith("data:"):
        # Strip the data URI prefix and decode the raw bytes
        header, _, b64data = settings.favicon_b64.partition(";base64,")
        mime = header.removeprefix("data:")
        import base64 as _b64
        content = _b64.b64decode(b64data)
        return Response(content=content, media_type=mime)
    # Fallback: transparent 1×1 ICO
    ICO = (
        b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
        b"\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00"
        b"\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
        b"\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\xff\xff\xff\x00\x00\x00"
    )
    return Response(content=ICO, media_type="image/x-icon")


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return PlainTextResponse(
        "User-agent: *\nAllow: /md/\nAllow: /llms.txt\n",
        media_type="text/plain; charset=utf-8",
    )


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt(request: Request, db=Depends(get_db)):
    from oparl_bridge.api.md import _fmt_date_short, _footer
    from oparl_bridge.db.models import Meeting, Organization, Paper, Person
    from oparl_bridge.wikidata import get_wikidata

    base = str(request.base_url).rstrip("/")
    wd = await get_wikidata(settings.wikidata_id) if settings.wikidata_id else None

    orgs = db.query(Organization).order_by(Organization.name).all()
    meetings = db.query(Meeting).order_by(Meeting.start.desc()).all()
    papers = db.query(Paper).order_by(Paper.id.desc()).all()
    persons = db.query(Person).order_by(Person.name).all()

    org_meeting_count = {}
    for mtg in meetings:
        if mtg.organization_id:
            org_meeting_count[mtg.organization_id] = org_meeting_count.get(mtg.organization_id, 0) + 1

    org_by_id = {o.id: o for o in orgs}

    lines = [
        f"# {settings.body_name} — Ratsinformationssystem",
        f"> Vollständiger Inhaltsindex. Alle Seiten als Markdown verfügbar unter {base}/md/.",
        "",
    ]

    if wd:
        if wd.bundesland or wd.landkreis:
            parts = []
            if wd.landkreis:
                lurl = f"https://www.wikidata.org/wiki/{wd.landkreis_qid}" if wd.landkreis_qid else None
                parts.append(f"[{wd.landkreis}]({lurl})" if lurl else wd.landkreis)
            if wd.bundesland:
                burl = f"https://www.wikidata.org/wiki/{wd.bundesland_qid}" if wd.bundesland_qid else None
                parts.append(f"[{wd.bundesland}]({burl})" if burl else wd.bundesland)
            lines.append(f"> Lage: {' · '.join(parts)}")
        if wd.population:
            lines.append(f"> Einwohner: {wd.population:,}".replace(",", "."))
        if wd.mayor:
            lines.append(f"> Bürgermeister/in: {wd.mayor}")
        if wd.ags:
            lines.append(f"> AGS: {wd.ags}")
        lines += [
            f"> Wikidata: {wd.wikidata_url}",
            *([ f"> Wikipedia: {wd.wikipedia_de}" ] if wd.wikipedia_de else []),
            "",
        ]

    lines += [
        "## Überblick",
        f"- {len(orgs)} Gremien",
        f"- {len(meetings)} Sitzungen",
        f"- {len(papers)} Vorlagen",
        f"- {len(persons)} Personen",
        "",
        f"## Gremien ({len(orgs)})",
    ]

    for org in orgs:
        count = org_meeting_count.get(org.id, 0)
        if count == 0:
            continue
        label = org.name
        if org.organization_type:
            label += f" ({org.organization_type})"
        lines.append(f"- [{label}]({base}/md/gremien/{org.id}) — {count} Sitzungen")

    recent_meetings = meetings[:50]
    lines += ["", f"## Aktuelle Sitzungen (50 von {len(meetings)}, [alle]({base}/md/sitzungen/))"]
    for mtg in recent_meetings:
        org_name = org_by_id[mtg.organization_id].name if mtg.organization_id and mtg.organization_id in org_by_id else ""
        date_str = _fmt_date_short(mtg.start)
        label = f"{date_str} — {org_name}" if org_name else date_str
        lines.append(f"- [{label}]({base}/md/sitzungen/{mtg.id})")

    lines += [
        "",
        f"## Vorlagen ({len(papers)})",
        f"Alle {len(papers)} Vorlagen sind einzeln abrufbar unter `{base}/md/vorlagen/{{id}}`.",
        "",
        f"## Personen ({len(persons)})",
        f"[Vollständige Personenliste mit Mitgliedschaften]({base}/md/personen/)",
    ]

    lines.append(_footer())
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request, db=Depends(get_db)):
    from oparl_bridge.db.models import Meeting, Organization, Paper
    base = str(request.base_url).rstrip("/")

    orgs = db.query(Organization).all()
    meetings = db.query(Meeting).order_by(Meeting.start.desc()).all()
    papers = db.query(Paper).all()

    def url(loc: str, lastmod: str | None = None, changefreq: str | None = None) -> str:
        parts = [f"  <url>\n    <loc>{loc}</loc>"]
        if lastmod:
            parts.append(f"    <lastmod>{lastmod}</lastmod>")
        if changefreq:
            parts.append(f"    <changefreq>{changefreq}</changefreq>")
        parts.append("  </url>")
        return "\n".join(parts)

    def isodate(dt) -> str | None:
        return dt.strftime("%Y-%m-%d") if dt else None

    entries = [
        url(f"{base}/md/", changefreq="weekly"),
        url(f"{base}/md/sitzungen/", changefreq="weekly"),
        url(f"{base}/md/personen/", changefreq="monthly"),
        url(f"{base}/llms.txt", changefreq="weekly"),
    ]
    for org in orgs:
        entries.append(url(f"{base}/md/gremien/{org.id}", isodate(org.scraped_at), "monthly"))
    for mtg in meetings:
        lm = isodate(mtg.detail_scraped_at or mtg.scraped_at)
        entries.append(url(f"{base}/md/sitzungen/{mtg.id}", lm))
    for p in papers:
        entries.append(url(f"{base}/md/vorlagen/{p.id}", isodate(p.scraped_at)))

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/")
async def root():
    return FileResponse(_STATIC / "index.html")


def main():
    uvicorn.run("oparl_bridge.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
