# oparl-bridge — Project Context for AI Sessions

## What this is
oparl-bridge is an OParl 1.1-compatible API gateway that scrapes German municipal
information systems running on ALLRIS (cc-eGov/Somacos) and exposes them as a
standards-compliant OParl REST API with a browser UI, LLM-crawler-friendly Markdown
endpoints, and Wikidata normative data integration.

## Why it exists
~1,500 German municipalities use ALLRIS, which is built on Apache Wicket — a Java
framework that renders content via Ajax callbacks, making data invisible to most
scrapers and all LLM crawlers. oparl-bridge acts as a compatibility layer.

## Architecture
```
ALLRIS (Wicket/Ajax)  →  Playwright scraper  →  SQLite (metadata)  →  FastAPI (OParl API + MD endpoints)
```
- **Scraper**: `src/oparl_bridge/scraper/` — async Playwright, navigates Wicket UI
- **Normalizer**: `src/oparl_bridge/normalizer/` — ALLRIS → OParl 1.1 Pydantic models
- **DB**: `src/oparl_bridge/db/` — SQLAlchemy + SQLite, metadata only (~50MB)
- **API**: `src/oparl_bridge/api/routes.py` — FastAPI, OParl 1.1 REST endpoints
- **UI API**: `src/oparl_bridge/api/ui.py` — denormalised endpoints for the SPA (not OParl-compliant)
- **MD API**: `src/oparl_bridge/api/md.py` — Markdown endpoints for LLM crawlers (`/md/*`)
- **SPA**: `src/oparl_bridge/static/index.html` — Alpine.js + PicoCSS browser UI served at `/`
- **Sync**: `src/oparl_bridge/sync.py` — orchestrates scrape → persist
- **CLI**: `src/oparl_bridge/cli.py` — `oparl-bridge-sync` command
- **Wikidata**: `src/oparl_bridge/wikidata.py` — SPARQL fetch + JSON file cache (TTL 1 week)

PDFs are NOT fetched or stored — only `accessUrl` pointing back to ALLRIS.

## Development target
The local `.env` (gitignored) points to `https://www.neu-wulmstorf.de/allris/`.
Do not hardcode this URL anywhere in the source — it belongs in `.env` only.

## ALLRIS URL patterns (verified against Neu Wulmstorf)
| Path | Description | Scraped? |
|------|-------------|----------|
| `/allris/gr010` | Committee list — columns: Name \| Mitglieder \| Letzte Sitzung \| Nächste Sitzung | ✅ |
| `/allris/gr020?GRLFDNR=<id>` | Committee detail | ❌ |
| `/allris/si010` | Meeting calendar (all committees) | ✅ |
| `/allris/si018?GRLFDNR=<id>` | Meetings for one committee — columns: Datum \| Uhrzeit \| Sitzung \| Rang | ✅ |
| `/allris/to010?SILFDNR=<id>&refresh=false` | Meeting detail + agenda — dt labels: Betreff, Datum, Uhrzeit, Raum, Ort; each TOP has a +/- expand button (Wicket Ajax) that reveals Beschluss, Abstimmungsergebnis, Anlagen/Wortbeiträge | ✅ |
| `/allris/to020?TOLFDNR=<id>` | Agenda item detail — dt labels: Betreff; sections: Beschlussart (`#toBeschlussart`), Beschlusstext, Abstimmungsergebnis, Wortprotokoll (all inside `.compFull .docPart`); Anlagen via `a.attlink.pdf[href*=attachment-link]` | ✅ |
| `/allris/vo020?VOLFDNR=<id>` | Paper/Vorlage detail — dt labels: Betreff, Vorlageart; PDFs via `a[href*='.pdf']` | ✅ |
| `/allris/doc/<id>` | PDF documents (static) | ❌ |

**Note:** The meeting detail page is `to010`, not `si020`. Direct links on si018 point to `to010`.

**Session notes:**
- to010, to020, vo020, si018 all require a warmed-up ALLRIS session (httpx with cookie jar).
- Call `_http_get("/gr010")` (or equivalent) before any detail page — otherwise networkidle hangs.
- to020 additionally requires a prior to010 visit **in the same httpx session** to establish Wicket navigation state. Use `prepare_for_to020(meeting_id)` before scraping to020 items for a meeting.
- **nichtöffentlich items** (number starts with `N`, or name contains "nichtöffentlich") always return 302 from to020 — skip them without making a request.
- WebFetch/curl will get 403 or redirect. Scraper uses Playwright for Wicket-Ajax pages (to010, si018) and httpx for static detail pages (to020, vo020, PDF proxy).

## OParl objects (implementation priority)
1. `oparl:System` ✅
2. `oparl:Body` ✅ (includes `ags`, `equivalent` from Wikidata)
3. `oparl:Organization` ✅
4. `oparl:Meeting` ✅
5. `oparl:AgendaItem` ✅
6. `oparl:Paper` ✅
7. `oparl:File` ✅

## Configuration (env vars with `OPARL_` prefix)
| Variable | Default | Description |
|----------|---------|-------------|
| `OPARL_ALLRIS_BASE_URL` | *(required)* | ALLRIS instance URL — set in `.env` |
| `OPARL_DATABASE_URL` | `sqlite:///./oparl_bridge.db` | SQLAlchemy DB URL |
| `OPARL_API_BASE_URL` | `http://localhost:8000` | Fallback only — API derives URLs from the incoming request automatically |
| `OPARL_BODY_NAME` | *(required)* | Municipality name — set in `.env` |
| `OPARL_BODY_WEBSITE` | *(required)* | Municipality website — set in `.env` |
| `OPARL_SYSTEM_NAME` | `Bürgerinformationssystem` | Label shown in the SPA header and browser title |
| `OPARL_WIKIDATA_ID` | *(optional)* | Wikidata QID (e.g. `Q508054`) — enables Wappen image, Wikipedia link, population, AGS, Landkreis/Bundesland, mayor, GND, OSM, GeoNames |
| `OPARL_FAVICON_B64` | *(optional)* | Base64 data URI for favicon (e.g. `data:image/x-icon;base64,...`) — generate ICO via cairosvg+Pillow |
| `OPARL_SCRAPER_HEADLESS` | `true` | Run browser headless |
| `OPARL_SCRAPER_DELAY_MS` | `1500` | Pause between requests (rate limiting) |

## Common commands
```bash
# Install
uv sync
uv run playwright install chromium

# Run API server
uv run oparl-bridge

# Sync data from ALLRIS
uv run oparl-bridge-sync sync-orgs       # committees only
uv run oparl-bridge-sync sync            # full sync (orgs → meetings → details → papers → item details)
uv run oparl-bridge-sync sync-papers     # papers/files only (details must already be scraped)
uv run oparl-bridge-sync sync-item-details  # to020 scraping (Beschluss, Wortbeitrag, Anlagen)
uv run oparl-bridge-sync reset-details   # reset detail_scraped_at → force re-scrape all meeting details

# Validate OParl JSON schema compliance
uv run python scripts/validate_oparl.py

# Tests
uv run --extra dev python -m pytest

# Lint
uv run --extra dev ruff check src/
```

## UI API endpoints (non-OParl, SPA-facing)
| Endpoint | Description |
|----------|-------------|
| `GET /` | Serves `static/index.html` (Alpine.js SPA) |
| `GET /ui/all` | All meetings + agenda items in one response — used for client-side search index |
| `GET /ui/meeting/{id}` | Meeting with inlined agenda items, papers, and file URLs |
| `GET /ui/proxy/file/{id}` | Fetches PDF from ALLRIS via httpx (session cookie + Referer) |
| `GET /ui/admin/recent` | Top 100 most recent scrape events across all sync types (to010/to020/vo020), with type badges and meeting links |

## Markdown endpoints (LLM-crawler-friendly)
| Endpoint | Description |
|----------|-------------|
| `GET /md/` | Index of all committees + municipality Wikidata normdata |
| `GET /md/gremien/{id}` | Committee with full meeting list |
| `GET /md/sitzungen/` | All meetings (200 most recent) |
| `GET /md/sitzungen/{id}` | Full meeting: agenda, Beschlüsse, Wortbeiträge, Anlagen |
| `GET /md/vorlagen/{id}` | Paper with files and consultation history |
| `GET /md/personen/` | All persons with linked memberships |
| `GET /robots.txt` | Allows `/md/` and `/llms.txt` |
| `GET /llms.txt` | Full content index with Wikidata context |
| `GET /sitemap.xml` | Standard sitemap of all `/md/*` URLs with lastmod |
| `GET /favicon.ico` | Serves `OPARL_FAVICON_B64` or transparent 1×1 ICO fallback |

All `/md/*` responses include `Link: canonical` and `Last-Modified` headers.
OParl JSON responses include `x-markdownUrl` field and `Link: alternate; type="text/markdown"` header.

## Wikidata integration
`src/oparl_bridge/wikidata.py` fetches from Wikidata SPARQL at startup and caches in `wikidata_cache.json` (gitignored, TTL 1 week, stale-while-revalidate — never blocks requests).

Properties fetched: P439 (AGS), P6 (mayor), P1082 (population), P2046 (area), P625 (coordinates), P227 (GND), P402 (OSM relation), P1566 (GeoNames), P131+ chain → Landkreis (Q106658) + Bundesland (Q200250), dewiki sitelink.

Used in: OParl `body.ags`, `body.equivalent`, `/md/` index page, `llms.txt`.

## Key design decisions
- **One Body per instance**: oparl-bridge is deployed per ALLRIS instance; Body ID is always `/oparl/v1.1/body/1`
- **IDs from ALLRIS**: OParl numeric IDs are the ALLRIS integer IDs (GRLFDNR, SILFDNR, etc.)
- **No PDF storage**: Files have `accessUrl` pointing to ALLRIS Wicket resource URLs (`/allris/wicket/resource/org.apache.wicket.Application/doc<id>.pdf`). These are NOT static `/allris/doc/<id>` paths — they're Wicket dynamic resources.
- **Pydantic v2**: All OParl objects validated via Pydantic; use `model_dump(by_alias=True)` for JSON output
- **SQLAlchemy 2.0 style**: Use `Mapped[]` type annotations, not legacy `Column()`
- **URL derivation**: `OParlMapper` is instantiated per-request via FastAPI dependency injection, using `request.base_url` so URLs in responses reflect the actual hostname/scheme
- **Rate limiting**: `AllrisScraper._goto()` sleeps `OPARL_SCRAPER_DELAY_MS` before every `page.goto()` call; `_http_get()` sleeps the same delay before every httpx request.
- **Incremental sync**: `Meeting.detail_scraped_at` tracks whether to010 has been scraped. `sync_meeting_details` only scrapes meetings where this is NULL. `AgendaItem.result_scraped_at` tracks whether to020 has been scraped; `sync_agenda_item_details` only scrapes items where this is NULL.
- **to020 session grouping**: `sync_agenda_item_details` groups pending items by `meeting_id` and calls `prepare_for_to020(meeting_id)` once per group (visits to010 via httpx to prime Wicket navigation state) before scraping to020 for any item in that meeting.
- **nichtöffentlich skip**: AgendaItems with `number` starting with `N` or `name` containing "nichtöffentlich" always return 302 from to020. Skip them instantly (set `result_scraped_at`, no HTTP request).
- **Paper sync**: `sync_papers` collects distinct `paper_id` values from `AgendaItem` rows not yet in the `Paper` table, scrapes `vo020`, and stores `Paper` + `File` records.
- **SQLite optimizations**: WAL journal mode + `synchronous=NORMAL` + 32 MB page cache + 128 MB mmap + `temp_store=MEMORY` set via SQLAlchemy `connect` event in `db/session.py`. Seven `CREATE INDEX IF NOT EXISTS` statements in `_migrate()` cover `meetings(organization_id, detail_scraped_at)`, `agenda_items(meeting_id, result_scraped_at, paper_id)`, `files(meeting_id, agenda_item_id)`. All applied automatically on first `init_db()` call — existing DBs are upgraded on next server start.
- **SQLite migrations**: `init_db()` calls `_migrate()` which uses `ALTER TABLE` to add new columns and `CREATE INDEX IF NOT EXISTS` for indexes. No Alembic.
- **dt-only parsing**: `_parse_meeting_detail` and `_parse_paper` query only `dt` elements, not `th`. The agenda table on to010 has a `th` named "Betreff" which would overwrite the correctly parsed meeting name.
- **AgendaItem paper link**: `AgendaItem.paper_id` (VOLFDNR FK) and `paper_reference` (link text) are populated from to010 cells[4] during `sync_meeting_details`.
- **PDF proxy**: Uses httpx — visit the source page (vo020/to010/to020) to obtain a JSESSIONID cookie and Referer URL, then GET the Wicket resource URL directly with `Referer` set. httpx handles the session cookie automatically. Wicket resource URLs (`/allris/wicket/resource/org.apache.wicket.Application/doc*.pdf`) are stable across sessions; only the Referer check enforced by ALLRIS requires the source-page visit.
- **AgendaItem attachments (to010)**: Clicking the Wicket `+` expand button per TOP during `scrape_meeting_detail` reveals Beschlusstext, Abstimmungsergebnis, Anlagen, and Wortbeiträge. `_derive_result` maps raw German text to OParl result enums.
- **AgendaItem details (to020)**: `_parse_to020_bs` extracts Beschlussart (`#toBeschlussart`), and for each `a[data-simpletooltip-text]` inside `.compFull`: Beschlusstext (tip contains "beschluss"), Abstimmungsergebnis (tip contains "abstimmung"), Wortprotokoll (tip contains "wortprotokoll"/"wortbeitrag") from `.docPart` divs. Anlagen via `a.attlink.pdf[href*=attachment-link]`.
- **Sentinel URLs**: `allris://to020/{tolfdnr}/{index}` for Wicket download-only Anlagen. `_display_url()` converts to display URL; `displayLabel` carries filename.
- **Wikidata cache**: JSON file, never in DB (avoids SQLite locking conflicts with sync process). `get_wikidata()` returns cached data instantly and fires background refresh via `asyncio.ensure_future` if stale.
- **si018 pagination**: ALLRIS paginates at 25 meetings. Wicket `pageLink` callback URLs must be triggered via `page.evaluate()` + `dispatchEvent`, not `fill()`. Results deduplicated by SILFDNR.
- **SPA search**: `/ui/all` fetched once in background. All filtering client-side (Alpine.js). No server requests per keystroke.
- **Responsive layouts**: Two Alpine.js breakpoint variables: `wide = window.innerWidth > 1024` (meeting split-panel), `wideList = window.innerWidth > 640` (all list views). Below 640 px all tables hide (`x-show="...wideList"`) and `.mlist-item` card lists appear (`x-show="...!wideList"`). Meeting detail wrapper uses `display:contents` so `.split-layout` inherits `height:100%` from `body > main`. Height/overflow CSS scoped to `@media (min-width: 1025px)` to preserve native mobile scroll.
- **`_parse_paper_bs` title fallback**: Falls back to `<title>` only if the title doesn't contain "ratsinformationssystem" (which is ALLRIS's generic page title when Betreff is absent).
- **Future meeting dedup**: `showMeetings()` in the SPA deduplicates `futureMeetingDates` against scraped meetings by comparing the first 10 characters (date only). `next_meeting_date` from gr010 is a bare `YYYY-MM-DD` string — comparing 16 chars (datetime) would miss the match against a scraped meeting with a full timestamp.

## OParl spec
https://dev.oparl.org/spezifikation/
