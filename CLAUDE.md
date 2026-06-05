# oparl-bridge — Project Context for AI Sessions

## What this is
oparl-bridge is an OParl 1.1-compatible API gateway that scrapes German municipal
information systems running on ALLRIS (cc-eGov/Somacos) and exposes them as a
standards-compliant OParl REST API. This enables OParl frontends like
meine-stadt-transparent to work with any ALLRIS instance without modification.

## Why it exists
~1,500 German municipalities use ALLRIS, which is built on Apache Wicket — a Java
framework that renders content via Ajax callbacks, making data invisible to most
scrapers and all LLM crawlers. oparl-bridge acts as a compatibility layer.

## Architecture
```
ALLRIS (Wicket/Ajax)  →  Playwright scraper  →  SQLite (metadata)  →  FastAPI (OParl API)
```
- **Scraper**: `src/oparl_bridge/scraper/` — async Playwright, navigates Wicket UI
- **Normalizer**: `src/oparl_bridge/normalizer/` — ALLRIS → OParl 1.1 Pydantic models
- **DB**: `src/oparl_bridge/db/` — SQLAlchemy + SQLite, metadata only (~50MB)
- **API**: `src/oparl_bridge/api/` — FastAPI, OParl 1.1 REST endpoints
- **Sync**: `src/oparl_bridge/sync.py` — orchestrates scrape → persist
- **CLI**: `src/oparl_bridge/cli.py` — `oparl-bridge-sync` command

PDFs are NOT fetched or stored — only `accessUrl` pointing back to ALLRIS.

## Development target
```
https://www.neu-wulmstorf.de/allris/
```

## ALLRIS URL patterns (consistent across all instances)
| Path | Description |
|------|-------------|
| `/allris/gr010` | Committee list (Gremienübersicht) |
| `/allris/gr020?GRLFDNR=<id>` | Committee detail |
| `/allris/si010` | Meeting calendar (Sitzungskalender) |
| `/allris/si018?GRLFDNR=<id>` | Meetings for one committee |
| `/allris/si020?SILFDNR=<id>` | Meeting detail + agenda |
| `/allris/to020?TOLFDNR=<id>` | Agenda item detail |
| `/allris/vo020?VOLFDNR=<id>` | Paper/Vorlage detail |
| `/allris/doc/<id>` | PDF documents (static) |

ALLRIS uses Apache Wicket — pages require a browser session (JS/cookies).
WebFetch/curl will get 403. Always use Playwright for scraping.

## OParl objects (implementation priority)
1. `oparl:System` ✅
2. `oparl:Body` ✅
3. `oparl:Organization` ✅
4. `oparl:Meeting` ✅
5. `oparl:AgendaItem` ✅
6. `oparl:Paper` ✅
7. `oparl:File` ✅

## Configuration (env vars with `OPARL_` prefix)
| Variable | Default | Description |
|----------|---------|-------------|
| `OPARL_ALLRIS_BASE_URL` | `https://www.neu-wulmstorf.de/allris` | ALLRIS instance URL |
| `OPARL_DATABASE_URL` | `sqlite:///./oparl_bridge.db` | SQLAlchemy DB URL |
| `OPARL_API_BASE_URL` | `http://localhost:8000` | Public API base URL |
| `OPARL_BODY_NAME` | `Gemeinde Neu Wulmstorf` | Municipality name |
| `OPARL_BODY_WEBSITE` | `https://www.neu-wulmstorf.de` | Municipality website |
| `OPARL_SCRAPER_HEADLESS` | `true` | Run browser headless |

## Common commands
```bash
# Install
pip install -e ".[dev]"
playwright install chromium

# Run API server
oparl-bridge
# or: uvicorn oparl_bridge.main:app --reload

# Sync data from ALLRIS
oparl-bridge-sync sync-orgs   # committees only
oparl-bridge-sync sync        # full sync

# Tests
pytest
```

## Key design decisions
- **One Body per instance**: oparl-bridge is deployed per ALLRIS instance; Body ID is always `/oparl/v1.1/body/1`
- **IDs from ALLRIS**: OParl numeric IDs are the ALLRIS integer IDs (GRLFDNR, SILFDNR, etc.)
- **No PDF storage**: Files have `accessUrl` pointing to ALLRIS `/allris/doc/<id>` endpoints
- **Pydantic v2**: All OParl objects validated via Pydantic; use `model_dump(by_alias=True)` for JSON output
- **SQLAlchemy 2.0 style**: Use `Mapped[]` type annotations, not legacy `Column()`

## OParl spec
https://dev.oparl.org/spezifikation/
