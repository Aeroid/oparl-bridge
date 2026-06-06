"""Wikidata normative data fetch + JSON file cache (TTL: 1 week)."""

import asyncio
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

WIKIDATA_TTL = timedelta(weeks=1)
_SPARQL_URL = "https://query.wikidata.org/sparql"
_UA = "oparl-bridge/1.0 (https://github.com/aeroid/oparl-bridge)"
_CACHE_FILE = Path("wikidata_cache.json")


@dataclasses.dataclass
class WikidataData:
    qid: str
    ags: str | None = None
    mayor: str | None = None
    population: int | None = None
    area: float | None = None
    lat: float | None = None
    lon: float | None = None
    gnd: str | None = None
    osm_id: str | None = None
    geonames_id: str | None = None
    landkreis: str | None = None
    landkreis_qid: str | None = None
    bundesland: str | None = None
    bundesland_qid: str | None = None
    wikipedia_de: str | None = None

    @property
    def wikidata_url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"

    @property
    def equivalent_urls(self) -> list[str]:
        urls = [self.wikidata_url]
        if self.wikipedia_de:
            urls.append(self.wikipedia_de)
        if self.gnd:
            urls.append(f"https://d-nb.info/gnd/{self.gnd}")
        return urls


def _read_cache(qid: str) -> WikidataData | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(_CACHE_FILE.read_text())
        if payload.get("qid") != qid:
            return None
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if datetime.now(UTC).replace(tzinfo=None) - fetched_at >= WIKIDATA_TTL:
            return None  # stale — caller decides whether to use anyway
        d = payload["data"]
        return WikidataData(**d)
    except Exception:
        return None


def _read_cache_any(qid: str) -> WikidataData | None:
    """Return cached data regardless of TTL (for stale-while-revalidate)."""
    if not _CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(_CACHE_FILE.read_text())
        if payload.get("qid") != qid:
            return None
        return WikidataData(**payload["data"])
    except Exception:
        return None


def _write_cache(data: WikidataData) -> None:
    payload = {
        "qid": data.qid,
        "fetched_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "data": dataclasses.asdict(data),
    }
    _CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _sparql(qid: str) -> str:
    return f"""
SELECT DISTINCT ?ags ?mayor ?mayorLabel ?population ?area ?lat ?lon
       ?gnd ?osm ?geonames
       ?landkreis ?landkreisLabel ?bundesland ?bundeslandLabel ?wikipedia
WHERE {{
  BIND(wd:{qid} AS ?item)
  OPTIONAL {{ ?item wdt:P439 ?ags. }}
  OPTIONAL {{ ?item wdt:P6 ?mayor. }}
  OPTIONAL {{ ?item wdt:P1082 ?population. }}
  OPTIONAL {{ ?item wdt:P2046 ?area. }}
  OPTIONAL {{
    ?item p:P625/psv:P625 ?coord.
    ?coord wikibase:geoLatitude ?lat.
    ?coord wikibase:geoLongitude ?lon.
  }}
  OPTIONAL {{ ?item wdt:P227 ?gnd. }}
  OPTIONAL {{ ?item wdt:P402 ?osm. }}
  OPTIONAL {{ ?item wdt:P1566 ?geonames. }}
  OPTIONAL {{
    ?item wdt:P131+ ?landkreis.
    ?landkreis wdt:P31/wdt:P279* wd:Q106658.
  }}
  OPTIONAL {{
    ?item wdt:P131+ ?bundesland.
    ?bundesland wdt:P31 wd:Q200250.
  }}
  OPTIONAL {{
    ?wikipedia schema:about ?item;
               schema:isPartOf <https://de.wikipedia.org/>.
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "de,en". }}
}}
LIMIT 1
"""


def _qid_from_uri(uri: str | None) -> str | None:
    if uri and "/entity/" in uri:
        return uri.rsplit("/", 1)[1]
    return None


def _parse(qid: str, result: dict) -> WikidataData:
    bindings = result.get("results", {}).get("bindings", [])
    if not bindings:
        logger.warning("Wikidata SPARQL returned no results for %s", qid)
        return WikidataData(qid=qid)
    b = bindings[0]

    def v(key: str) -> str | None:
        return b.get(key, {}).get("value") or None

    pop, area, lat, lon = v("population"), v("area"), v("lat"), v("lon")
    return WikidataData(
        qid=qid,
        ags=v("ags"),
        mayor=v("mayorLabel"),
        population=int(float(pop)) if pop else None,
        area=round(float(area), 2) if area else None,
        lat=round(float(lat), 6) if lat else None,
        lon=round(float(lon), 6) if lon else None,
        gnd=v("gnd"),
        osm_id=v("osm"),
        geonames_id=v("geonames"),
        landkreis=v("landkreisLabel"),
        landkreis_qid=_qid_from_uri(v("landkreis")),
        bundesland=v("bundeslandLabel"),
        bundesland_qid=_qid_from_uri(v("bundesland")),
        wikipedia_de=v("wikipedia"),
    )


async def _fetch_and_cache(qid: str) -> WikidataData:
    async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=30.0) as client:
        resp = await client.get(_SPARQL_URL, params={"query": _sparql(qid), "format": "json"})
        resp.raise_for_status()
        data = _parse(qid, resp.json())
    _write_cache(data)
    logger.info("Wikidata refreshed for %s (AGS=%s, Landkreis=%s)", qid, data.ags, data.landkreis)
    return data


async def get_wikidata(qid: str) -> WikidataData | None:
    """Return fresh cached data instantly; trigger background refresh if stale/missing."""
    fresh = _read_cache(qid)
    if fresh:
        return fresh
    stale = _read_cache_any(qid)
    # Schedule background refresh; return stale data (or None on first ever call)
    asyncio.ensure_future(_fetch_and_cache(qid))
    return stale


async def warm_cache(qid: str) -> None:
    """Fetch at startup if cache is empty or stale."""
    if _read_cache(qid) is None:
        try:
            await _fetch_and_cache(qid)
        except Exception:
            logger.exception("Wikidata warm_cache failed for %s", qid)
