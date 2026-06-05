# oparl-bridge

An OParl 1.1-compatible API gateway for German municipal information systems running on **ALLRIS** (cc-eGov/Somacos).

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
  FastAPI OParl API      ← standard OParl 1.1 REST API
        ↓
meine-stadt-transparent  ← (or any OParl-compatible frontend)
```

PDFs are **not** fetched or stored. Files only carry an `accessUrl` pointing back to the ALLRIS instance.

## Supported ALLRIS objects → OParl mapping

| ALLRIS | OParl |
|--------|-------|
| Gremium (gr010/gr020) | `oparl:Organization` |
| Sitzung (si010/si020) | `oparl:Meeting` |
| Tagesordnungspunkt (to020) | `oparl:AgendaItem` |
| Vorlage/Drucksache (vo020) | `oparl:Paper` |
| Dokument (doc/) | `oparl:File` (accessUrl only) |

## Requirements

- Python 3.12+
- Playwright Chromium

## Installation

```bash
# Clone the repo
git clone https://github.com/aeroid/oparl-bridge.git
cd oparl-bridge

# Install with pip (or uv)
pip install -e ".[dev]"

# Install Playwright browser
playwright install chromium
```

## Configuration

All settings use the `OPARL_` prefix, readable from environment variables or a `.env` file:

```env
OPARL_ALLRIS_BASE_URL=https://www.your-municipality.de/allris
OPARL_API_BASE_URL=https://oparl.your-municipality.de
OPARL_BODY_NAME=Stadt Musterstadt
OPARL_BODY_WEBSITE=https://www.your-municipality.de
OPARL_DATABASE_URL=sqlite:///./oparl_bridge.db
OPARL_SCRAPER_HEADLESS=true
```

To use a different ALLRIS instance, only `OPARL_ALLRIS_BASE_URL` and `OPARL_BODY_NAME` need to change.

## Running

### 1. Initial data sync

```bash
# Sync committees from gr010
oparl-bridge-sync sync-orgs

# Full sync (committees → meetings per committee)
oparl-bridge-sync sync
```

### 2. Start the API server

```bash
oparl-bridge
# or: uvicorn oparl_bridge.main:app --reload
```

The OParl entrypoint is at: `http://localhost:8000/oparl/v1.1/`

Interactive API docs: `http://localhost:8000/docs`

## OParl endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /oparl/v1.1/` | System object |
| `GET /oparl/v1.1/bodies` | Body list |
| `GET /oparl/v1.1/body/1` | Body detail |
| `GET /oparl/v1.1/body/1/organizations` | All committees |
| `GET /oparl/v1.1/organization/{id}` | Committee detail |
| `GET /oparl/v1.1/body/1/meetings` | All meetings (filter: `?organization=<id>`) |
| `GET /oparl/v1.1/meeting/{id}` | Meeting detail |
| `GET /oparl/v1.1/agendaitem/{id}` | Agenda item detail |
| `GET /oparl/v1.1/body/1/papers` | All papers/Vorlagen |
| `GET /oparl/v1.1/paper/{id}` | Paper detail |
| `GET /oparl/v1.1/file/{id}` | File metadata (accessUrl to ALLRIS) |

## Development

```bash
# Run tests
pytest

# Run with auto-reload
uvicorn oparl_bridge.main:app --reload
```

## Architecture notes

- **One deployment per ALLRIS instance.** The Body ID is always `/oparl/v1.1/body/1`.
- **ALLRIS IDs are preserved** as OParl numeric IDs (GRLFDNR → Organization.id, etc.).
- **Playwright is required** because ALLRIS uses Apache Wicket with Ajax rendering — static HTTP requests return 403 or empty pages.

## License

MIT
