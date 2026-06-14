"""ALLRIS Windows App XML API client (undocumented endpoint /app01).

Discovered by capturing traffic from the ALLRIS Windows app (v1.2.15.0).

Startup sequence (mirrored from Windows app):
  1. GET /allris/info.xml          — server version check (no auth)
  2. GET /app01?action=1&...       — system metadata; sets JSESSIONID cookie
  3. GET /app01?action=2&firstdate=DD.MM.YYYY&timestamp=0  — change feed
  4. GET /app01?action=4&SILFDNR=N — meeting detail XML (one per meeting)

action=4 returns 302 → /allris/noauth for nichtöffentlich (auth-restricted) meetings.
"""

import asyncio
import logging
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

from oparl_bridge.config import Settings
from oparl_bridge.config import settings as default_settings
from oparl_bridge.scraper.base import ScrapedAgendaItem, ScrapedFile, ScrapedMeeting

logger = logging.getLogger(__name__)


class MeetingRestrictedError(Exception):
    """Raised when action=4 returns 302 — meeting is nichtöffentlich (auth-gated)."""


_APP_UA = "ALLRIS/1.2.15.0 (Win; App)"

# Document types from action=1 that are publicly accessible (skip 117=Protokoll nichtöffentlich etc.)
_PUBLIC_DOC_TYPES = {103, 104, 105, 108, 109, 110, 114, 116, 134, 135, 136, 142, 143, 144, 145}

# beschlussart bitmask values from action=1 <beschlussarten> → OParl result enum.
# totyp=0 means "no decision yet" → not in map → returns None.
_TOTYP_RESULT: dict[int, str] = {
    1: "ACCEPTED",    # ungeändert beschlossen
    2: "ACCEPTED",    # geändert beschlossen
    4: "NODECISION",  # zur Kenntnis genommen
    8: "DEFERRED",    # verwiesen
    16: "DEFERRED",   # vertagt
    32: "REJECTED",   # abgelehnt
    64: "DEFERRED",   # zurückgezogen
}


def _minutes_to_hm(minutes: int) -> tuple[int, int]:
    return minutes // 60, minutes % 60


def _juldat_to_iso(juldat: str, beginn: int | None = None) -> str | None:
    """Convert 'DD.MM.YYYY' + optional minutes-since-midnight to ISO 8601."""
    try:
        dt = datetime.strptime(juldat, "%d.%m.%Y")
        if beginn is not None:
            h, m = _minutes_to_hm(beginn)
            dt = dt.replace(hour=h, minute=m)
        return dt.isoformat()
    except (ValueError, AttributeError):
        return None


def _folge_to_number(folge: ET.Element) -> str | None:
    """Convert <folge num unum uunum knum> to display number like '3.1' or '4.1.2'."""
    num = int(folge.get("num", "0"))
    if not num:
        return None
    parts = [str(num)]
    unum = int(folge.get("unum", "0"))
    if unum:
        parts.append(str(unum))
        uunum = int(folge.get("uunum", "0"))
        if uunum:
            parts.append(str(uunum))
    return ".".join(parts)


def _cdata(elem: ET.Element, tag: str) -> str | None:
    """Return stripped text of a child element, or None if absent/empty."""
    child = elem.find(tag)
    if child is not None and child.text:
        return child.text.strip() or None
    return None


def _is_html(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("<!DOCTYPE") or t.startswith("<html") or "<html" in t[:200]


def _doc_url(base: str, guid: str, typ: int) -> str:
    """Build the real ALLRIS document download URL from a <dokument> element.

    Verified from Windows app traffic:
      GET /allris/doc?DOLFDNR={guid}&DOCTYP={typ}&OTYP=41&crc=1
    Response includes X-Checksum header matching crc= from the XML.
    Requires JSESSIONID cookie (from App API session warmup).
    """
    return f"{base}/doc?DOLFDNR={guid}&DOCTYP={typ}&OTYP=41&crc=1"


class AllrisAppApi:
    """Client for the ALLRIS Windows App XML API (endpoint: {base}/app01).

    Must be used as an async context manager:

        async with AllrisAppApi() as api:
            info = await api.get_system_info()
            result = await api.get_session_xml(silfdnr=1000417)
    """

    def __init__(self, cfg: Settings = default_settings) -> None:
        self.cfg = cfg
        self._http: httpx.AsyncClient | None = None
        self._warmed_up: bool = False
        self._warmup_xml: ET.Element | None = None

    async def __aenter__(self) -> "AllrisAppApi":
        # follow_redirects=False so we can detect the 302→/allris/noauth pattern
        # (nichtöffentlich meetings) without making an extra request.
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=30.0,
            headers={"User-Agent": _APP_UA},
        )
        self._warmed_up = False
        return self

    async def __aexit__(self, *_) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._warmed_up = False
        self._warmup_xml = None

    async def _delay(self) -> None:
        """Enforce rate limiting before every outgoing HTTP request."""
        if self.cfg.app_api_delay_ms > 0:
            await asyncio.sleep(self.cfg.app_api_delay_ms / 1000)

    async def _warmup(self) -> None:
        """Obtain a JSESSIONID via action=1 and cache the parsed XML.

        Called once on first use and again after session expiry.
        The caller is responsible for calling _delay() before _warmup() if needed.
        The action=1 response is stored in _warmup_xml so get_system_info() can
        reuse it without a second request.
        """
        if self._warmed_up or self._http is None:
            return
        base = self.cfg.allris_base_url.rstrip("/")
        resp = await self._http.get(
            f"{base}/app01",
            params={
                "action": "1",
                "template": "app1",
                "custom": "Vorlage,Kommunalpolitiker,Parlament,Konferenz",
            },
        )
        self._warmed_up = True
        text = resp.text.strip() if resp.status_code == 200 else ""
        if text and not _is_html(text):
            try:
                self._warmup_xml = ET.fromstring(text)
            except ET.ParseError:
                pass

    async def _get_xml(self, params: dict, retries: int = 2) -> ET.Element | None:
        """Fetch /app01 with the given params and return the parsed XML root.

        Every request — including the initial warmup — is preceded by _delay() so
        the configured rate limit is always respected.

        Returns None for:
        - 302 redirect (nichtöffentlich meeting, auth-gated)
        - HTML response (session expired or NOLIS fallback)
        - XML parse error
        """
        assert self._http is not None, "Use inside `async with AllrisAppApi()` block"
        # Warmup (first call only): delay + action=1 to obtain JSESSIONID.
        if not self._warmed_up:
            await self._delay()
            await self._warmup()
        url = f"{self.cfg.allris_base_url.rstrip('/')}/app01"
        action = params.get("action", "?")
        silfdnr = params.get("SILFDNR", "")
        _req_label = f"/app01 action={action}" + (f" SILFDNR={silfdnr}" if silfdnr else "")
        # action=4 (per-meeting) is logged by sync.py with date/org context; only log here for bulk actions
        if not silfdnr:
            logger.debug("▶ %s", _req_label)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            await self._delay()
            try:
                resp = await self._http.get(url, params=params)
                # 302 → /allris/noauth means this meeting is permanently auth-restricted
                if resp.is_redirect:
                    raise MeetingRestrictedError(
                        f"action={params.get('action')} SILFDNR={params.get('SILFDNR')} → 302"
                    )
                resp.raise_for_status()
                text = resp.text.strip()
                if _is_html(text):
                    # NOLIS fallback or session expired → re-warm and retry once
                    if attempt == 1:
                        logger.warning(
                            "app01 action=%s returned HTML (session expired?), re-warming",
                            params.get("action"),
                        )
                        self._warmed_up = False
                        await self._delay()
                        await self._warmup()
                        continue
                    logger.debug("app01 action=%s still HTML after re-warm", params.get("action"))
                    return None
                return ET.fromstring(text)
            except MeetingRestrictedError:
                raise  # 302 is permanent, no retry
            except ET.ParseError as exc:
                logger.warning("app01 XML parse error (action=%s): %s", params.get("action"), exc)
                return None
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(2.0)
        if last_exc is not None:
            raise last_exc
        return None

    async def check_server(self) -> str | None:
        """GET /allris/info.xml and return the server version string, or None on failure.

        The Windows app calls this before action=1 to verify the server is reachable.
        Does not require authentication.
        """
        assert self._http is not None
        await self._delay()
        try:
            resp = await self._http.get(
                f"{self.cfg.allris_base_url.rstrip('/')}/info.xml",
                headers={"Cache-Control": "no-cache"},
            )
            if resp.status_code != 200:
                return None
            root = ET.fromstring(resp.text)
            server = root.find("server")
            return server.get("version") if server is not None else None
        except Exception:
            return None

    async def get_system_info(self) -> dict:
        """Fetch action=1 system metadata (gremien, beschlussarten, doc types, …).

        Reuses the XML cached during _warmup() if available, avoiding a second request.
        """
        if not self._warmed_up:
            await self._delay()
            await self._warmup()
        root = self._warmup_xml or await self._get_xml({
            "action": "1",
            "template": "app1",
            "custom": "Vorlage,Kommunalpolitiker,Parlament,Konferenz",
        })
        if root is None:
            return {}

        result: dict = {
            "api_level": root.get("apiLevel"),
            "data_level": root.get("dataLevel"),
            "max_zurueck": int(root.get("maxZurueck", "0")) or None,
            "gremien": [],
            "beschlussarten": {},
            "dokumenttypen": {},
        }

        for g in root.findall(".//gremien/gremium"):
            result["gremien"].append({
                "grlfdnr": int(g.get("grlfdnr", "0")),
                "name": _cdata(g, "grname"),
                "short": _cdata(g, "grkurz"),
                "typ": g.get("typ"),
                "aktiv": g.get("aktiv") == "1",
            })

        for b in root.findall(".//beschlussarten/beschlussart"):
            balfdnr = int(b.get("balfdnr", "0"))
            name_el = b.find("name")
            result["beschlussarten"][balfdnr] = (
                name_el.text.strip() if name_el is not None and name_el.text else ""
            )

        for d in root.findall(".//dokumenttyp"):
            did = int(d.get("id", "0"))
            result["dokumenttypen"][did] = d.get("name", "")

        return result

    async def get_changed_meetings(self, since: datetime) -> list[dict]:
        """Fetch action=2 change feed — returns meetings modified since `since`.

        Verified parameter format from Windows app traffic:
          firstdate=DD.MM.YYYY  (date part only)
          timestamp=DD.MM.YYYY HH:MM:SS  (full datetime of last sync)

        Response: <sitzungen timestamp="DD.MM.YYYY HH:MM:SS">
          <sitzung silfdnr="..." timestamp="..." dotimestamp="..." />
        </sitzungen>

        `timestamp` = last modification of the meeting record itself.
        `dotimestamp` = last modification of the meeting's documents ("0" if none).

        Returns list of dicts: {silfdnr, timestamp, dotimestamp} — raw strings as
        returned by the server for comparison with stored values.
        """
        firstdate = since.strftime("%d.%m.%Y")
        timestamp = since.strftime("%d.%m.%Y %H:%M:%S")
        root = await self._get_xml({
            "action": "2",
            "template": "app2",
            "firstdate": firstdate,
            "timestamp": timestamp,
        })
        if root is None or root.tag != "sitzungen":
            return []
        results = []
        for s in root.findall("sitzung"):
            silfdnr_str = s.get("silfdnr", "")
            if silfdnr_str.isdigit():
                results.append({
                    "silfdnr": int(silfdnr_str),
                    "timestamp": s.get("timestamp", ""),
                    "dotimestamp": s.get("dotimestamp", "0"),
                })
        return results

    async def get_session_xml(
        self, silfdnr: int
    ) -> tuple[ScrapedMeeting, list[ScrapedAgendaItem]] | None:
        """Fetch action=4 for one meeting.

        Returns (ScrapedMeeting, [ScrapedAgendaItem]) or None if:
        - the meeting is auth-restricted (nichtöffentlich) → 302
        - the SILFDNR doesn't exist
        - the API returns unusable data
        """
        root = await self._get_xml({
            "action": "4",
            "template": "app4",
            "SILFDNR": str(silfdnr),
        })
        if root is None or root.tag != "sitzung":
            return None

        if root.get("silfdnr") != str(silfdnr):
            logger.warning("app01 action=4: silfdnr mismatch (requested %d)", silfdnr)

        # --- Meeting header ---
        datum_el = root.find("datum")
        start: str | None = None
        if datum_el is not None:
            juldat = datum_el.get("juldat", "")
            beginn_str = datum_el.get("beginn", "")
            # beginn=0 means time unknown (nichtöffentlich section), treat as None
            beginn = int(beginn_str) if beginn_str.isdigit() and beginn_str != "0" else None
            start = _juldat_to_iso(juldat, beginn)

        # Organization: first gremium in the meeting's own <gremien> (not inside <top>)
        org_id: int | None = None
        for g in root.findall("gremien/gremium"):
            try:
                org_id = int(g.get("grlfdnr", "0")) or None
            except (ValueError, TypeError):
                pass
            break

        # Location: raum/raname + geb (building + address)
        location: str | None = None
        raum_el = root.find("raum")
        if raum_el is not None:
            parts = [p for p in [_cdata(raum_el, "raname"), _cdata(raum_el, "geb")] if p]
            location = ", ".join(parts) or None

        # <text> is the descriptive title; <siname> is the reference code like "BAU/036/2025"
        name = _cdata(root, "text") or _cdata(root, "siname") or ""

        # Meeting-level documents (Bekanntmachung, Protokoll, etc.)
        base = self.cfg.allris_base_url.rstrip("/")
        meeting_files: list[ScrapedFile] = []
        sitzung_docs: list[dict] = []
        for dok in root.findall("dokumente/dokument"):
            guid = dok.get("guid", "")
            typ_str = dok.get("typ", "0")
            typ = int(typ_str) if typ_str.isdigit() else 0
            if not guid:
                continue
            sitzung_docs.append({
                "guid": guid,
                "typ": typ,
                "dotimestamp": dok.get("dotimestamp"),
                "crc": dok.get("crc"),
            })
            if typ not in _PUBLIC_DOC_TYPES:
                continue
            meeting_files.append(ScrapedFile(
                name=f"DOC{guid}.pdf",
                url=_doc_url(base, guid, typ),
                crc=dok.get("crc") or None,
                guid=guid,
                typ=typ,
            ))

        meeting = ScrapedMeeting(
            id=silfdnr,
            name=name,
            organization_id=org_id,
            start=start,
            location=location,
            files=meeting_files,
            docs=sitzung_docs,
        )

        # --- TOPs → AgendaItems ---
        items: list[ScrapedAgendaItem] = []
        for top in root.findall("tops/top"):
            lfdnr_str = top.get("lfdnr", "")
            if not lfdnr_str.isdigit():
                continue
            tolfdnr = int(lfdnr_str)

            ost = top.get("ost", "1")
            public = ost == "1"

            betreff = _cdata(top, "betreff") or ""

            folge_el = top.find("folge")
            number = _folge_to_number(folge_el) if folge_el is not None else None

            paper_id: int | None = None
            paper_reference: str | None = None
            result: str | None = None

            beratung_str = top.get("beratung", "")
            beratung_id = int(beratung_str) if beratung_str.isdigit() else None

            beschluss_datum: str | None = None
            beschluss_totyp: int | None = None
            protokoll_guid: str | None = None
            protokoll_crc: str | None = None

            vorlage_el = top.find("vorlage")
            if vorlage_el is not None:
                volfdnr_str = vorlage_el.get("volfdnr", "")
                if volfdnr_str.isdigit():
                    paper_id = int(volfdnr_str)
                paper_reference = _cdata(vorlage_el, "voname")

                # <beschluesse> contains ALL past decisions for this Vorlage across all
                # meetings. Find the entry for THIS meeting (sinr == silfdnr) to get
                # the correct result — not the first entry, which may be from a prior meeting.
                this_sinr = str(silfdnr)
                for beschluss in vorlage_el.findall(".//beschluss"):
                    if beschluss.get("sinr") == this_sinr:
                        totyp_str = beschluss.get("totyp", "")
                        if totyp_str.isdigit():
                            totyp_int = int(totyp_str)
                            result = _TOTYP_RESULT.get(totyp_int)
                            beschluss_totyp = totyp_int
                        # Convert DD.MM.YYYY → ISO date
                        raw_datum = beschluss.get("datum", "")
                        if raw_datum:
                            try:
                                from datetime import datetime as _dt
                                beschluss_datum = _dt.strptime(raw_datum, "%d.%m.%Y").strftime("%Y-%m-%d")
                            except ValueError:
                                pass
                        # Protokollauszug (typ=138) attached to this Beschluss
                        prot_dok = beschluss.find('dokument[@typ="138"]')
                        if prot_dok is not None:
                            protokoll_guid = prot_dok.get("guid") or None
                            protokoll_crc = prot_dok.get("crc") or None
                        break

            # TOP-level documents (Anlagen)
            top_files: list[ScrapedFile] = []
            for dok in top.findall("dokument"):
                guid = dok.get("guid", "")
                typ_str = dok.get("typ", "0")
                typ = int(typ_str) if typ_str.isdigit() else 0
                if not guid:
                    continue
                top_files.append(ScrapedFile(
                    name=f"DOC{guid}.pdf",
                    url=_doc_url(base, guid, typ),
                    crc=dok.get("crc") or None,
                    guid=guid,
                    typ=typ,
                ))

            items.append(ScrapedAgendaItem(
                id=tolfdnr,
                name=betreff,
                number=number,
                public=public,
                paper_id=paper_id,
                paper_reference=paper_reference,
                result=result,
                files=top_files,
                beratung_id=beratung_id,
                beschluss_datum=beschluss_datum,
                beschluss_totyp=beschluss_totyp,
                protokoll_guid=protokoll_guid,
                protokoll_crc=protokoll_crc,
            ))

        if not items and not name:
            return None

        return meeting, items
