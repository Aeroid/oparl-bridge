# oparl-bridge

An OParl 1.1-compatible API gateway for German municipal information systems running on **ALLRIS** (cc-eGov/Somacos). Includes a browser UI for browsing meetings, agendas, and documents.

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
  FastAPI OParl API      ← standard OParl 1.1 REST API + browser UI
        ↓
meine-stadt-transparent  ← (or any OParl-compatible frontend)
```

PDFs are **not** fetched or stored. Files carry an `accessUrl` back to ALLRIS; the browser UI proxies PDFs on demand by launching a headless Playwright browser, navigating to the paper page, and intercepting the PDF download — the only approach that works with Wicket's session-scoped resource URLs.

## Supported ALLRIS objects → OParl mapping

| ALLRIS | OParl |
|--------|-------|
| Gremium (gr010) | `oparl:Organization` |
| Sitzung (si018) | `oparl:Meeting` |
| Sitzungsdetail + Tagesordnung (to010) | `oparl:Meeting` (location, agenda) |
| Tagesordnungspunkt + aufgeklappter Beschluss | `oparl:AgendaItem` (result, resolutionText, auxiliaryFile) |
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
OPARL_SCRAPER_DELAY_MS=1500   # pause between requests — be a good citizen
```

`OPARL_API_BASE_URL` is only a fallback. The API derives URLs from the incoming HTTP request automatically, so it works correctly behind reverse proxies.

## Running

### 1. Sync data from ALLRIS

```bash
# Full sync: committees → meetings → meeting details (location, agenda) → papers/files
uv run oparl-bridge-sync sync

# Sync committees only (also refreshes session cookies for the PDF proxy)
uv run oparl-bridge-sync sync-orgs
```

### 2. Start the server

```bash
uv run oparl-bridge
```

| URL | What you get |
|-----|-------------|
| `http://localhost:8000/` | Browser UI (Alpine.js SPA) |
| `http://localhost:8000/oparl/v1.1/` | OParl API entrypoint |
| `http://localhost:8000/docs` | Interactive API docs |

## Browser UI

The SPA at `/` provides a navigable view of the scraped data:

- **Gremien** → click → **Sitzungsliste** (newest first) → click → **Tagesordnung**
- Each agenda item shows its TOP number, title, Vorlage reference (e.g. `VO/25/04351`), and direct PDF links
- **Search**: live full-text search across all meetings, agenda items, and Vorlage references — index loads in the background, all filtering is client-side (no server requests per keystroke)
- **Beschlüsse**: each agenda item shows a result badge (beschlossen / abgelehnt / vertagt / zur Kenntnis), the raw Abstimmungsergebnis, and Beschlusstext scraped from the ALLRIS expand panel
- **Wide screens (>1500 px)**: PDF opens in a split-view iframe panel; narrower screens open PDFs in a new tab

PDFs are served via `/ui/proxy/file/{id}`. Each request launches a headless Playwright browser, navigates to the relevant ALLRIS page (vo020 for Vorlage files, to010 for agenda-item attachments), and intercepts the PDF download via route interception. This is ~3–5 s per click but requires no pre-stored cookies.

## OParl endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /oparl/v1.1/` | System object |
| `GET /oparl/v1.1/bodies` | Body list |
| `GET /oparl/v1.1/body/1` | Body detail |
| `GET /oparl/v1.1/body/1/organizations` | All committees |
| `GET /oparl/v1.1/organization/{id}` | Committee detail |
| `GET /oparl/v1.1/body/1/meetings` | All meetings (`?organization=<id>` to filter) |
| `GET /oparl/v1.1/meeting/{id}` | Meeting with agenda item URLs |
| `GET /oparl/v1.1/agendaitem/{id}` | Agenda item with paper consultation link |
| `GET /oparl/v1.1/body/1/papers` | All Vorlagen |
| `GET /oparl/v1.1/paper/{id}` | Paper with mainFile / auxiliaryFile links |
| `GET /oparl/v1.1/file/{id}` | File metadata with `accessUrl` to ALLRIS |

## Sync commands

```bash
uv run oparl-bridge-sync sync           # full sync
uv run oparl-bridge-sync sync-orgs      # committees only (also refreshes session cookies)
uv run oparl-bridge-sync sync-papers    # papers/files only (meeting details must exist)
uv run oparl-bridge-sync reset-details  # force re-scrape of all meeting detail pages
```

## Development

```bash
uv run --extra dev python -m pytest
uv run --extra dev ruff check src/
```

## Architecture notes

- **One deployment per ALLRIS instance.** The Body ID is always `/oparl/v1.1/body/1`.
- **ALLRIS IDs are preserved** as OParl numeric IDs (GRLFDNR → Organization.id, etc.).
- **Playwright is required** — ALLRIS uses Apache Wicket with Ajax; static HTTP returns 403.
- **Session warmup**: scraper must visit `gr010` before detail pages are accessible.
- **Rate limiting**: configurable delay between every `page.goto()` call (`OPARL_SCRAPER_DELAY_MS`).
- **Incremental sync**: `Meeting.detail_scraped_at` tracks scraped meetings; only new ones are re-scraped.
- **SQLite migrations**: new columns are added via `ALTER TABLE` in `_migrate()` — no Alembic.

## License

MIT
