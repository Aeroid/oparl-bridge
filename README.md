# oparl-bridge

An OParl 1.1-compatible API gateway for German municipal information systems running on **ALLRIS** (cc-eGov/Somacos). Includes a browser UI, LLM-crawler-friendly Markdown endpoints, and Wikidata normative data integration.

## The problem

~1,500 German municipalities use ALLRIS to publish council decisions, meeting agendas, and official documents. ALLRIS is built on Apache Wicket — a Java framework that renders content through Ajax callbacks, making it **invisible to standard scrapers, LLM crawlers, and OParl-compatible frontends** like [meine-stadt-transparent](https://github.com/meine-stadt-transparent/meine-stadt-transparent).

## The solution

oparl-bridge sits between ALLRIS and OParl-compatible tools:

```
ALLRIS (Wicket/Ajax)
        ↓
  Playwright scraper     ← navigates the actual browser UI
        ↓
    SQLite cache         ← metadata only, ~50 MB for a small municipality
        ↓
  FastAPI OParl API      ← OParl 1.1 REST API + browser UI + Markdown endpoints
        ↓
meine-stadt-transparent  ← (or any OParl-compatible frontend or LLM crawler)
```

PDFs are **not** fetched or stored. Files carry an `accessUrl` back to ALLRIS; the browser UI proxies PDFs on demand via Playwright route interception.

## Supported ALLRIS objects → OParl mapping

| ALLRIS | OParl |
|--------|-------|
| Gremium (gr010) | `oparl:Organization` |
| Sitzung (si018) | `oparl:Meeting` |
| Sitzungsdetail + Tagesordnung (to010) | `oparl:Meeting` (location, agenda) |
| Tagesordnungspunkt + Beschluss/Wortbeitrag | `oparl:AgendaItem` (result, resolutionText, auxiliaryFile) |
| Vorlage/Drucksache (vo020) | `oparl:Paper` |
| Dokument (Wicket resource URL) | `oparl:File` (accessUrl only) |

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Playwright Chromium

## Installation

```bash
git clone https://github.com/aeroid/oparl-bridge.git
cd oparl-bridge

uv sync
uv run playwright install chromium
```

## Configuration

Create a `.env` file (never committed):

```env
OPARL_ALLRIS_BASE_URL=https://www.your-municipality.de/allris
OPARL_BODY_NAME=Stadt Musterstadt
OPARL_BODY_WEBSITE=https://www.your-municipality.de
OPARL_DATABASE_URL=sqlite:///./oparl_bridge.db
OPARL_SCRAPER_HEADLESS=true
OPARL_SCRAPER_DELAY_MS=1500       # pause between requests — be a good citizen
OPARL_WIKIDATA_ID=Q12345          # optional: Wikidata QID for normative data
OPARL_FAVICON_B64=data:image/x-icon;base64,...  # optional: base64-encoded favicon ICO
```

`OPARL_API_BASE_URL` is only a fallback. The API derives URLs from the incoming HTTP request automatically, so it works correctly behind reverse proxies.

## Running

### 1. Sync data from ALLRIS

```bash
# Full sync: committees → meetings → meeting details (location, agenda) → papers/files
uv run oparl-bridge-sync sync

# Individual steps
uv run oparl-bridge-sync sync-orgs      # committees only
uv run oparl-bridge-sync sync-papers    # papers/files only (meeting details must exist)
uv run oparl-bridge-sync reset-details  # force re-scrape all meeting detail pages
```

### 2. Start the server

```bash
uv run oparl-bridge
```

| URL | What you get |
|-----|-------------|
| `http://localhost:8000/` | Browser UI (Alpine.js SPA) |
| `http://localhost:8000/oparl/v1.1/` | OParl 1.1 API entrypoint |
| `http://localhost:8000/md/` | Markdown index (LLM-crawler-friendly) |
| `http://localhost:8000/llms.txt` | LLM index with full content overview |
| `http://localhost:8000/sitemap.xml` | XML sitemap of all Markdown pages |
| `http://localhost:8000/docs` | Interactive API docs |

## Browser UI

The SPA at `/` provides a navigable view of the scraped data:

- **Gremien** → **Sitzungsliste** → **Tagesordnung** with agenda items, Vorlagen, and PDFs
- **Search**: live full-text search across all meetings, agenda items, and Vorlage references — client-side, no server requests per keystroke
- **Beschlüsse**: result badge (beschlossen / abgelehnt / vertagt / zur Kenntnis), Abstimmungsergebnis, Beschlusstext, Wortbeiträge
- **Wide screens (>1500 px)**: split-view PDF iframe panel; narrower screens open PDFs in a new tab
- **MD badge**: every detail view links to its Markdown equivalent

PDFs are served via `/ui/proxy/file/{id}`. The proxy visits the source page (vo020/to010/to020) via httpx to obtain a session cookie and Referer URL, then fetches the Wicket resource URL directly — typically under 1 s.

## Markdown endpoints (LLM-crawler-friendly)

Human- and machine-readable views of all data, with proper HTTP metadata:

| Endpoint | Description |
|----------|-------------|
| `GET /md/` | Index of all committees + municipality normdata |
| `GET /md/gremien/{id}` | Committee with full meeting list |
| `GET /md/sitzungen/` | All meetings (200 most recent) |
| `GET /md/sitzungen/{id}` | Full meeting: agenda, Beschlüsse, Wortbeiträge, Anlagen |
| `GET /md/vorlagen/{id}` | Paper with files and consultation history |
| `GET /md/personen/` | All persons with linked memberships |

All Markdown responses include:
- `Link: <url>; rel="canonical"` header
- `Last-Modified` header based on `scraped_at`
- Footer with source attribution and Wikidata CC0 notice

## OParl endpoints

OParl JSON responses include `x-markdownUrl` and `Link: alternate; type="text/markdown"` headers pointing to the corresponding Markdown page.

| Endpoint | Description |
|----------|-------------|
| `GET /oparl/v1.1/` | System object |
| `GET /oparl/v1.1/bodies` | Body list |
| `GET /oparl/v1.1/body/1` | Body detail (includes `ags`, `equivalent` from Wikidata) |
| `GET /oparl/v1.1/body/1/organizations` | All committees |
| `GET /oparl/v1.1/organization/{id}` | Committee detail |
| `GET /oparl/v1.1/body/1/meetings` | All meetings (`?organization=<id>` to filter) |
| `GET /oparl/v1.1/meeting/{id}` | Meeting with agenda item URLs |
| `GET /oparl/v1.1/agendaitem/{id}` | Agenda item with paper consultation link |
| `GET /oparl/v1.1/body/1/papers` | All Vorlagen |
| `GET /oparl/v1.1/paper/{id}` | Paper with mainFile / auxiliaryFile links |
| `GET /oparl/v1.1/file/{id}` | File metadata with `accessUrl` to ALLRIS |

## Wikidata integration

When `OPARL_WIKIDATA_ID` is set, oparl-bridge fetches normative data via SPARQL at startup and caches it locally (`wikidata_cache.json`, TTL 1 week, stale-while-revalidate):

- AGS (Amtlicher Gemeindeschlüssel) → OParl `body.ags`
- Wikidata URI + Wikipedia URL + GND → OParl `body.equivalent`
- Landkreis, Bundesland, Bürgermeister/in, Einwohnerzahl, Fläche, Koordinaten → `/md/` index and `llms.txt`
- GND, OpenStreetMap relation ID, GeoNames ID → `/md/` index

## Crawler metadata

| URL | Purpose |
|-----|---------|
| `/robots.txt` | Allows `/md/` and `/llms.txt` |
| `/llms.txt` | Full content index with Wikidata context |
| `/sitemap.xml` | Standard sitemap of all `/md/*` URLs with `lastmod` |
| `/favicon.ico` | Serves `OPARL_FAVICON_B64` (ICO) or transparent 1×1 fallback |
| SPA `<head>` | `<link rel="alternate">` for `/md/` and `/llms.txt` |

## Development

```bash
uv run --extra dev python -m pytest
uv run --extra dev ruff check src/

# Validate OParl JSON schema compliance
uv run python scripts/validate_oparl.py
```

## Architecture notes

- **One deployment per ALLRIS instance.** Body ID is always `/oparl/v1.1/body/1`.
- **ALLRIS IDs are preserved** as OParl numeric IDs (GRLFDNR → Organization.id, etc.).
- **Playwright is required** — ALLRIS uses Apache Wicket with Ajax; static HTTP returns 403.
- **Session warmup**: scraper must visit `gr010` before detail pages are accessible.
- **Rate limiting**: configurable delay between every `page.goto()` call (`OPARL_SCRAPER_DELAY_MS`).
- **Incremental sync**: `Meeting.detail_scraped_at` tracks scraped meetings; only new ones are re-scraped.
- **SQLite migrations**: new columns are added via `ALTER TABLE` in `_migrate()` — no Alembic.
- **Wikidata cache**: single JSON file (`wikidata_cache.json`, gitignored), refreshed in background if stale.

## License

MIT
