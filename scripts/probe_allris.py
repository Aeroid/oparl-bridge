#!/usr/bin/env python3
"""Probe an ALLRIS instance for available API capabilities and restrictions.

Usage:
    python scripts/probe_allris.py https://www.neu-wulmstorf.de/allris/
    python scripts/probe_allris.py https://ratsinformation.example.de/ratsinfo/ --json
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

_APP_UA = "ALLRIS/1.2.15.0 (Win; App)"
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

OK = "✓"
WARN = "⚠"
FAIL = "✗"
SKIP = "–"

_quiet = False  # set to True in --json mode to suppress progress output


def _log(msg: str) -> None:
    """Write a progress line to stderr (suppressed in --json mode)."""
    if not _quiet:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_base(url: str) -> str:
    """Strip trailing slash; ensure no /allris suffix is double-counted."""
    return url.rstrip("/")


def _is_html(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("<!DOCTYPE") or t.startswith("<html") or "<html" in t[:200]


def _parse_xml(text: str) -> ET.Element | None:
    if not text or _is_html(text):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


async def check_info_xml(client: httpx.AsyncClient, base: str) -> dict:
    """GET /allris/info.xml — ALLRIS server version, no auth required."""
    url = f"{base}/info.xml"
    result = {"url": url, "ok": False, "version": None, "raw": None}
    try:
        resp = await client.get(url, follow_redirects=True)
        result["status"] = resp.status_code
        if resp.status_code == 200:
            root = _parse_xml(resp.text)
            if root is not None:
                result["ok"] = True
                server_el = root.find(".//server")
                result["version"] = (
                    server_el.get("version") if server_el is not None else root.get("version")
                )
                result["raw"] = resp.text.strip()
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def check_html_page(
    client: httpx.AsyncClient, base: str, path: str, label: str
) -> dict:
    """GET an ALLRIS HTML page and check whether it's publicly accessible."""
    url = f"{base}/{path.lstrip('/')}"
    result = {"url": url, "label": label, "ok": False, "redirect": None}
    try:
        resp = await client.get(url, follow_redirects=False)
        result["status"] = resp.status_code
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            is_html = "html" in ct or _is_html(resp.text)
            result["ok"] = is_html
            result["content_type"] = ct
        elif resp.is_redirect:
            result["redirect"] = resp.headers.get("location", "")
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def check_app_action1(client: httpx.AsyncClient, base: str) -> dict:
    """action=1: system metadata. Returns JSESSIONID, gremien, beschlussarten, maxZurueck."""
    url = f"{base}/app01"
    result = {"url": url, "ok": False}
    try:
        resp = await client.get(
            url,
            params={
                "action": "1",
                "template": "app1",
                "custom": "Vorlage,Kommunalpolitiker,Parlament,Konferenz",
            },
            follow_redirects=False,
        )
        result["status"] = resp.status_code
        if resp.is_redirect:
            result["redirect"] = resp.headers.get("location", "")
            return result
        root = _parse_xml(resp.text)
        if root is None:
            result["html_response"] = _is_html(resp.text)
            return result

        result["ok"] = True
        result["api_level"] = root.get("apiLevel")
        result["data_level"] = root.get("dataLevel")
        max_z = root.get("maxZurueck")
        result["max_zurueck"] = int(max_z) if max_z and max_z.isdigit() else None

        benutzer = root.find("benutzer")
        if benutzer is not None:
            result["auth"] = benutzer.get("auth")
            result["user_type"] = benutzer.get("type")
            result["user_name"] = (
                f"{benutzer.get('vorname', '')} {benutzer.get('name', '')}".strip()
                or None
            )

        gremien = root.findall(".//gremien/gremium")
        result["gremien_total"] = len(gremien)
        result["gremien_aktiv"] = sum(1 for g in gremien if g.get("aktiv") == "1")
        result["gremien_raw"] = [
            {"grlfdnr": int(g.get("grlfdnr", "0")), "aktiv": g.get("aktiv") == "1"}
            for g in gremien
        ]

        result["beschlussarten"] = len(root.findall(".//beschlussarten/beschlussart"))
        result["dokumenttypen"] = len(root.findall(".//dokumenttyp"))

        # Cookie check
        result["jsessionid"] = "JSESSIONID" in client.cookies

    except Exception as exc:
        result["error"] = str(exc)
    return result


async def check_app_action2(client: httpx.AsyncClient, base: str) -> dict:
    """action=2: change feed — returns list of changed meetings since a given date."""
    url = f"{base}/app01"
    since = datetime(2000, 1, 1)
    result = {"url": url, "ok": False, "since": since.isoformat()}
    try:
        resp = await client.get(
            url,
            params={
                "action": "2",
                "template": "app2",
                "firstdate": since.strftime("%d.%m.%Y"),
                "timestamp": since.strftime("%d.%m.%Y %H:%M:%S"),
            },
            follow_redirects=False,
        )
        result["status"] = resp.status_code
        if resp.is_redirect:
            result["redirect"] = resp.headers.get("location", "")
            return result
        root = _parse_xml(resp.text)
        if root is None:
            result["html_response"] = _is_html(resp.text)
            return result

        # root tag is <sitzungen>, direct children are <sitzung>
        sitzungen = root.findall("sitzung") or root.findall(".//sitzung")
        silfdnrs = sorted(int(s.get("silfdnr", "0")) for s in sitzungen if s.get("silfdnr"))
        result["ok"] = True
        result["count"] = len(silfdnrs)
        result["silfdnr_min"] = silfdnrs[0] if silfdnrs else None
        result["silfdnr_max"] = silfdnrs[-1] if silfdnrs else None
        result["server_timestamp"] = root.get("timestamp")

    except Exception as exc:
        result["error"] = str(exc)
    return result


async def check_app_action4(
    client: httpx.AsyncClient, base: str, silfdnr: int
) -> dict:
    """action=4: full meeting XML for a known SILFDNR."""
    url = f"{base}/app01"
    result = {"url": url, "silfdnr": silfdnr, "ok": False}
    try:
        resp = await client.get(
            url,
            params={"action": "4", "SILFDNR": str(silfdnr)},
            follow_redirects=False,
        )
        result["status"] = resp.status_code
        if resp.is_redirect:
            result["redirect"] = resp.headers.get("location", "")
            result["nichtoeffentlich"] = "/noauth" in result["redirect"]
            return result
        root = _parse_xml(resp.text)
        if root is None:
            result["html_response"] = _is_html(resp.text)
            return result

        sitzung = root.find(".//sitzung") if root.tag != "sitzung" else root
        if sitzung is not None:
            result["ok"] = True
            result["tops"] = len(sitzung.findall(".//tops/top"))
            result["docs"] = len(sitzung.findall(".//dokumente/dokument"))
            datum = sitzung.find("datum")
            result["datum"] = datum.get("juldat") if datum is not None else None

    except Exception as exc:
        result["error"] = str(exc)
    return result


async def _si018_oldest_for_gremium(scraper, grlfdnr: int) -> dict | None:
    """Open si018 for one gremium and find the oldest public meeting.

    Strategy: default sort is Datum descending (newest first). Navigate to the
    LAST page — that holds the oldest meetings. No sort-toggle needed (Wicket's
    wicket_orderUp state is not reliably set after AJAX refresh).

    Returns None if no historical (past) meetings found (e.g. all nichtöffentlich).
    """
    from datetime import date, datetime

    from oparl_bridge.scraper.base import _set_full_date_range

    today_date = date.today()

    def _is_past(s: str) -> bool:
        try:
            return datetime.strptime(s, "%d.%m.%Y").date() < today_date
        except (ValueError, TypeError):
            return False

    def _parse_rows(html_rows: list[dict]) -> list[dict]:
        return [r for r in html_rows if _is_past(r.get("date", ""))]

    _read_rows_js = """() => {
        const rows = document.querySelectorAll('tbody tr');
        const out = [];
        for (const row of Array.from(rows)) {
            const dateEl = row.querySelector('.wodat');
            if (!dateEl) continue;
            const link = row.querySelector('a[href*="SILFDNR"]');
            out.push({
                date: dateEl.innerText.trim(),
                silfdnr: link ? (link.href.match(/SILFDNR=(\d+)/)||[])[1] : null,
                linked: !!link
            });
        }
        return out;
    }"""

    page = await scraper._new_page()
    try:
        await scraper._goto(page, scraper._url(f"/si018?GRLFDNR={grlfdnr}"))
        await _set_full_date_range(page)
        await page.wait_for_selector("table caption", timeout=15000)

        total_pages = await page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="pageLink"]');
            const nums = Array.from(links).map(a => parseInt(a.innerText.trim())).filter(n => n > 0);
            return nums.length ? Math.max(...nums) : 1;
        }""")

        # Navigate to last page (= oldest meetings in descending sort)
        if total_pages > 1:
            clicked = await page.evaluate(f"""() => {{
                const links = document.querySelectorAll('a[href*="pageLink"]');
                for (const a of links) {{
                    if (a.innerText.trim() === '{total_pages}') {{
                        a.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            if clicked:
                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_selector("table caption", timeout=10000)

        rows_info = await page.evaluate(_read_rows_js)
        # Last page is in descending order → reverse to get oldest first
        rows_info = list(reversed(rows_info))
        past_rows = _parse_rows(rows_info)

        if not past_rows:
            # Last page only has future meetings — this gremium has no historical data
            return None

        oldest_any = past_rows[0]
        oldest_linked = next((r for r in past_rows if r.get("linked")), None)

        # If last page has no linked rows at all, check second-to-last page
        if oldest_linked is None and total_pages > 1:
            prev_page = total_pages - 1
            clicked = await page.evaluate(f"""() => {{
                const links = document.querySelectorAll('a[href*="pageLink"]');
                for (const a of links) {{
                    if (a.innerText.trim() === '{prev_page}') {{
                        a.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            if clicked:
                await page.wait_for_load_state("networkidle", timeout=20000)
                more = await page.evaluate(_read_rows_js)
                linked = next(
                    (r for r in reversed(more) if r.get("linked") and _is_past(r.get("date", ""))),
                    None,
                )
                if linked:
                    oldest_linked = linked

        return {
            "grlfdnr": grlfdnr,
            "total_pages": total_pages,
            "oldest_date": oldest_any["date"],
            "oldest_linked_date": oldest_linked["date"] if oldest_linked else None,
            "oldest_silfdnr": oldest_linked["silfdnr"] if oldest_linked else None,
            "oldest_linked": oldest_linked is not None,
        }
    finally:
        await page.close()


async def check_oldest_via_playwright(base: str, grlfdnr_candidates: list[int]) -> dict:
    """Try several gremien on si018 (ascending date sort) to find the oldest public meeting.

    Checks ALL candidates and returns the one with the globally oldest meeting.
    Committees with no past meetings (all nichtöffentlich) are skipped.
    """
    try:
        from datetime import datetime

        from oparl_bridge.config import Settings
        from oparl_bridge.scraper.base import AllrisScraper
    except ImportError:
        return {"ok": False, "skipped": "oparl_bridge not installed"}

    result: dict = {"ok": False, "method": "playwright+si018", "tried_gremien": []}
    best: dict | None = None
    best_date = None
    total = len(grlfdnr_candidates)
    try:
        cfg = Settings(allris_base_url=base + "/", scraper_headless=True, scraper_delay_ms=0)
        scraper = AllrisScraper(cfg)
        async with scraper.session():
            for i, grlfdnr in enumerate(grlfdnr_candidates, 1):
                _log(f"        GRLFDNR={grlfdnr:<8} ({i}/{total}) ...")
                found = await _si018_oldest_for_gremium(scraper, grlfdnr)
                result["tried_gremien"].append(grlfdnr)
                if found is None:
                    _log(f"        GRLFDNR={grlfdnr:<8}          → keine öffentlichen Sitzungen")
                    continue
                try:
                    d = datetime.strptime(found["oldest_date"], "%d.%m.%Y").date()
                except (ValueError, TypeError):
                    continue
                marker = ""
                if best_date is None or d < best_date:
                    best_date = d
                    best = found
                    marker = " ← älteste bisher"
                _log(f"        GRLFDNR={grlfdnr:<8}          → {found['oldest_date']}{marker}")
        if best is not None:
            result.update(best)
            result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def check_doc_download(
    client: httpx.AsyncClient, base: str, guid: str, typ: int
) -> dict:
    """Check if a document can be downloaded from /allris/doc."""
    url = f"{base}/doc?DOLFDNR={guid}&DOCTYP={typ}&OTYP=41&crc=1"
    result = {"url": url, "ok": False}
    try:
        resp = await client.head(url, follow_redirects=False)
        result["status"] = resp.status_code
        result["ok"] = resp.status_code == 200
        if resp.status_code == 200:
            result["content_type"] = resp.headers.get("content-type", "")
            result["x_checksum"] = resp.headers.get("X-Checksum")
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def probe(base_url: str) -> dict:
    base = _normalize_base(base_url)
    report: dict = {
        "base_url": base,
        "probed_at": datetime.utcnow().isoformat() + "Z",
    }

    async with httpx.AsyncClient(
        timeout=20.0,
        headers={"User-Agent": _APP_UA},
        follow_redirects=False,
    ) as client:
        # 1. info.xml — ALLRIS server version
        _log("  [1/4] Server-Version (info.xml) ...")
        report["info_xml"] = await check_info_xml(client, base)
        v = report["info_xml"].get("version", "?")
        _log(f"        → Version {v}" if report["info_xml"]["ok"] else "        → nicht erreichbar")

        # 2. HTML pages — public HTML scraping viability
        _log("  [2/4] HTML-Scraping (gr010, si010) ...")
        report["html"] = {
            "gr010": await check_html_page(client, base, "gr010", "Gremien-Liste"),
            "si010": await check_html_page(client, base, "si010", "Sitzungskalender"),
        }

        # 3. App API action=1 (also warms up JSESSIONID)
        _log("  [3/4] App API ...")
        _log("        action=1 (System-Info) ...")
        report["action1"] = await check_app_action1(client, base)

        # 4. App API action=2 (change feed — only works after action=1 sets cookie)
        if report["action1"]["ok"]:
            a1 = report["action1"]
            _log(f"        → {a1.get('gremien_aktiv')} Gremien, maxZurueck={a1.get('max_zurueck')}")
            _log("        action=2 (Change-Feed) ...")
            report["action2"] = await check_app_action2(client, base)
            if report["action2"].get("ok"):
                _log(f"        → {report['action2']['count']} Sitzungen")
        else:
            report["action2"] = {"ok": False, "skipped": "action=1 failed"}

        # 5. App API action=4 — try several SILFDNRs until a public one is found
        silfdnrs_to_try: list[int] = []
        a2 = report.get("action2", {})
        if a2.get("ok") and a2.get("count", 0) > 0:
            # Re-fetch to get the full list and sample across the range
            resp2 = await client.get(
                f"{base}/app01",
                params={
                    "action": "2",
                    "template": "app2",
                    "firstdate": "01.01.2000",
                    "timestamp": "01.01.2000 00:00:00",
                },
                follow_redirects=False,
            )
            root2 = _parse_xml(resp2.text)
            if root2 is not None:
                all_si = root2.findall("sitzung") or root2.findall(".//sitzung")
                all_ids = sorted(int(s.get("silfdnr", "0")) for s in all_si if s.get("silfdnr"))
                # Try: max, 3rd-from-last, 10th-from-last, middle
                candidates = []
                for idx in [-1, -3, -10, len(all_ids) // 2]:
                    try:
                        candidates.append(all_ids[idx])
                    except IndexError:
                        pass
                silfdnrs_to_try = list(dict.fromkeys(candidates))  # deduplicate, preserve order

        _log("        action=4 (Sitzungsdetail) ...")
        report["action4"] = {"ok": False, "skipped": "no SILFDNR from action=2"}
        silfdnr_used: int | None = None
        for silfdnr_candidate in silfdnrs_to_try:
            result4 = await check_app_action4(client, base, silfdnr_candidate)
            if result4.get("ok"):
                report["action4"] = result4
                silfdnr_used = silfdnr_candidate
                break
            elif result4.get("nichtoeffentlich"):
                # Record last nichtöffentlich result in case we never find a public one
                report["action4"] = result4
        if not silfdnrs_to_try:
            report["action4"] = {"ok": False, "skipped": "no SILFDNR from action=2"}
        if report["action4"].get("ok"):
            silfdnr_used = report["action4"]["silfdnr"]

        # 6. Historical depth placeholder — filled below after client closes
        report["oldest_meeting"] = {"ok": False, "skipped": "pending Playwright check"}

        # 7. Document download — try first doc from public action=4 result
        report["doc_download"] = {"ok": False, "skipped": "no document found"}
        if report.get("action4", {}).get("ok") and silfdnr_used:
            resp = await client.get(
                f"{base}/app01",
                params={"action": "4", "SILFDNR": str(silfdnr_used)},
                follow_redirects=False,
            )
            root = _parse_xml(resp.text) if not resp.is_redirect else None
            if root is not None:
                sitzung = root.find(".//sitzung") if root.tag != "sitzung" else root
                if sitzung is not None:
                    for dok in sitzung.findall(".//dokument"):
                        guid = dok.get("guid")
                        typ_str = dok.get("typ", "")
                        if guid and typ_str.isdigit():
                            report["doc_download"] = await check_doc_download(
                                client, base, guid, int(typ_str)
                            )
                            break

    # 6. Historical depth via Playwright si018 (last page = oldest meetings).
    # Check ALL active gremien with "normal" IDs (≤ 9999 = original committees, highest
    # historical coverage), plus up to 3 samples from the high-ID range (recent additions).
    # All candidates are tried; the globally oldest date wins.
    aktive = [g for g in report.get("action1", {}).get("gremien_raw", []) if g.get("aktiv")]
    aktive_sorted = sorted({g["grlfdnr"] for g in aktive})
    normal = [gid for gid in aktive_sorted if gid <= 9999]
    high = [gid for gid in aktive_sorted if gid > 9999]
    # Sample 3 from high-ID range (first, middle, last)
    if high:
        n = len(high)
        high_sample = list(dict.fromkeys([high[0], high[n // 2], high[-1]]))
    else:
        high_sample = []
    candidates = normal + high_sample if normal else (high_sample or [1])
    _log(f"  [4/4] Historische Abdeckung (Playwright, {len(candidates)} Gremien) ...")
    report["oldest_meeting"] = await check_oldest_via_playwright(base, candidates)

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _sym(ok: bool | None) -> str:
    if ok is True:
        return OK
    if ok is False:
        return FAIL
    return SKIP


def render_report(report: dict) -> None:
    base = report["base_url"]
    print()
    print(f"ALLRIS Probe: {base}")
    print("=" * 65)

    # --- 1. Server ---
    print()
    print("1) Server")
    ix = report.get("info_xml", {})
    if ix.get("ok"):
        v = ix.get("version") or "?"
        print(f"  {OK}  /info.xml erreichbar — ALLRIS Server Version {v}")
    else:
        status = ix.get("status", "–")
        err = ix.get("error", "")
        print(f"  {FAIL}  /info.xml nicht erreichbar (HTTP {status}{' – ' + err if err else ''})")

    # --- 2. HTML-Scraping ---
    print()
    print("2) HTML-Scraping (Playwright / httpx)")
    for key, label in [("gr010", "Gremien-Liste (gr010)"), ("si010", "Sitzungskalender (si010)")]:
        h = report.get("html", {}).get(key, {})
        status = h.get("status", "–")
        if h.get("ok"):
            print(f"  {OK}  {label} → HTTP {status} (öffentlich zugänglich)")
        elif h.get("redirect"):
            print(f"  {WARN}  {label} → HTTP {status}, Redirect → {h['redirect']}")
        else:
            err = h.get("error", "")
            print(f"  {FAIL}  {label} → HTTP {status}{' – ' + err if err else ''}")

    # --- 3. App API ---
    print()
    print("3) App API (/app01, User-Agent: ALLRIS/1.2.15.0)")
    a1 = report.get("action1", {})
    if a1.get("ok"):
        print(f"  {OK}  action=1 (System-Info) → HTTP {a1.get('status')}")
        print(f"       apiLevel={a1.get('api_level')}, dataLevel={a1.get('data_level')}")
        mz = a1.get("max_zurueck")
        if mz:
            print(f"       maxZurueck={mz} (Change-Feed nur letzte {mz} Jahre)")
        else:
            print("       maxZurueck=nicht gesetzt")
        auth = a1.get("auth", "?")
        utype = a1.get("user_type", "?")
        uname = a1.get("user_name") or "anonym"
        print(f"       Nutzer: auth={auth}, type={utype}, Name=\"{uname}\"")
        print(f"       Gremien: {a1.get('gremien_aktiv')} aktiv, {a1.get('gremien_total', 0) - (a1.get('gremien_aktiv') or 0)} inaktiv")
        print(f"       Beschlussarten: {a1.get('beschlussarten')}, Dokumenttypen: {a1.get('dokumenttypen')}")
        cookie_sym = OK if a1.get("jsessionid") else WARN
        print(f"       {cookie_sym}  JSESSIONID gesetzt: {a1.get('jsessionid')}")
    elif a1.get("redirect"):
        print(f"  {FAIL}  action=1 → Redirect ({a1.get('status')}) → {a1.get('redirect')}")
        print("       App API nicht verfügbar oder Login erforderlich")
    elif a1.get("html_response"):
        print(f"  {FAIL}  action=1 → HTML-Antwort (App API nicht unterstützt / NOLIS-Fallback)")
    else:
        err = a1.get("error", a1.get("status", "unbekannter Fehler"))
        print(f"  {FAIL}  action=1 → {err}")

    a2 = report.get("action2", {})
    if a2.get("skipped"):
        print(f"  {SKIP}  action=2 (Change-Feed) → übersprungen ({a2['skipped']})")
    elif a2.get("ok"):
        print(f"  {OK}  action=2 (Change-Feed) → {a2['count']} Sitzungen")
        print(f"       SILFDNR-Bereich: {a2['silfdnr_min']} – {a2['silfdnr_max']}")
        print(f"       Server-Timestamp: {a2.get('server_timestamp', '–')}")
    elif a2.get("redirect"):
        print(f"  {FAIL}  action=2 → Redirect → {a2['redirect']}")
    elif a2.get("html_response"):
        print(f"  {FAIL}  action=2 → HTML-Antwort (falsche Parameter oder nicht verfügbar)")
    else:
        print(f"  {FAIL}  action=2 → {a2.get('status', a2.get('error', '?'))}")

    a4 = report.get("action4", {})
    if a4.get("skipped"):
        print(f"  {SKIP}  action=4 (Sitzungsdetail) → übersprungen ({a4['skipped']})")
    elif a4.get("ok"):
        print(f"  {OK}  action=4 (SILFDNR={a4['silfdnr']}) → {a4['tops']} TOPs, {a4['docs']} Dokumente")
    elif a4.get("nichtoeffentlich"):
        print(f"  {WARN}  action=4 (SILFDNR={a4['silfdnr']}) → alle Kandidaten nichtöffentlich (302), prinzipiell verfügbar")
    elif a4.get("redirect"):
        print(f"  {WARN}  action=4 (SILFDNR={a4['silfdnr']}) → Redirect → {a4['redirect']}")
    else:
        print(f"  {FAIL}  action=4 → {a4.get('status', a4.get('error', '?'))}")

    dd = report.get("doc_download", {})
    if dd.get("skipped"):
        print(f"  {SKIP}  Dokument-Download → übersprungen ({dd['skipped']})")
    elif dd.get("ok"):
        print(f"  {OK}  Dokument-Download (/allris/doc?DOLFDNR=...) → HTTP {dd['status']}")
        if dd.get("x_checksum"):
            print(f"       X-Checksum: {dd['x_checksum']} (CRC-Verifikation möglich)")
    else:
        print(f"  {FAIL}  Dokument-Download → HTTP {dd.get('status', dd.get('error', '?'))}")

    # --- 3.5 Historical depth ---
    print()
    print("4) Historische Abdeckung (si018 letzte Seite via Playwright)")
    oh = report.get("oldest_meeting", {})
    if oh.get("skipped"):
        print(f"  {SKIP}  {oh['skipped']}")
    elif oh.get("error"):
        print(f"  {FAIL}  Playwright-Check fehlgeschlagen: {oh['error']}")
    elif oh.get("ok"):
        pages = oh.get("total_pages", "?")
        oldest_date = oh.get("oldest_date")
        oldest_linked = oh.get("oldest_linked_date")
        osid = oh.get("oldest_silfdnr")
        gid = oh.get("grlfdnr", "?")
        tried = oh.get("tried_gremien", [gid])
        tried_str = ", ".join(str(g) for g in tried)
        print(f"  {OK}  GRLFDNR={gid} (ausprobiert: {tried_str})")
        print(f"       Seiten gesamt: {pages}  (≈{pages if isinstance(pages, int) else '?'}×25 Sitzungen)")
        if oldest_date:
            if oldest_linked and oldest_linked == oldest_date:
                print(f"       Älteste Sitzung (verlinkt, SILFDNR={osid}): {oldest_date}")
            elif oldest_date and oldest_linked:
                print(f"       Älteste Sitzung gesamt (nicht verlinkt): {oldest_date}")
                print(f"       Älteste verlinkbare Sitzung (SILFDNR={osid}): {oldest_linked}")
            elif oldest_date and not oldest_linked:
                print(f"       Älteste Sitzung: {oldest_date} (kein SILFDNR-Link — nicht direkt scrapebar)")
        mz = report.get("action1", {}).get("max_zurueck")
        if mz:
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=365 * mz)).isoformat()
            print(f"       action=2-Cutoff (−{mz} Jahre): {cutoff}")
            # oldest_date is DD.MM.YYYY; cutoff is YYYY-MM-DD — parse properly
            from datetime import datetime as _dt2
            try:
                oldest_dt = _dt2.strptime(oldest_date, "%d.%m.%Y").date()
                cutoff_dt = _dt2.fromisoformat(cutoff).date()
                if oldest_dt < cutoff_dt:
                    print("       → Daten reichen über den action=2-Cutoff hinaus — Playwright si018 nötig für vollständige Historie")
            except (ValueError, TypeError):
                pass
    elif not oh.get("ok") and oh.get("tried_gremien"):
        tried = oh.get("tried_gremien", [])
        tried_str = ", ".join(str(g) for g in tried)
        print(f"  {WARN}  Keine historischen Sitzungen in GRLFDNR={tried_str} — alle nichtöffentlich?")
    else:
        print(f"  {WARN}  Keine Sitzung auf erster Seite gefunden (leere Liste?)")

    # --- 4. Einschränkungen & Empfehlung ---
    print()
    print("5) Einschränkungen")
    issues = []

    if not report.get("info_xml", {}).get("ok"):
        issues.append(f"{FAIL}  ALLRIS-Server nicht erreichbar oder keine ALLRIS-Instanz")

    html_gr010 = report.get("html", {}).get("gr010", {})
    if not html_gr010.get("ok"):
        if html_gr010.get("redirect"):
            issues.append(f"{WARN}  HTML-Scraping erfordert Login (gr010 gibt Redirect zurück)")
        else:
            issues.append(f"{FAIL}  HTML-Scraping nicht möglich (gr010 nicht zugänglich)")

    if not a1.get("ok"):
        issues.append(f"{FAIL}  App API (action=1) nicht verfügbar — kein inkrementeller Sync möglich")
    else:
        mz = a1.get("max_zurueck")
        if mz:
            issues.append(f"{WARN}  action=2 Change-Feed begrenzt auf letzte {mz} Jahre (maxZurueck={mz})")
        if a1.get("auth") == "0":
            issues.append(f"{WARN}  App API: kein Nutzer authentifiziert (auth=0) — nur öffentliche Daten")

    if not a2.get("ok") and not a2.get("skipped"):
        issues.append(f"{FAIL}  action=2 (Change-Feed) nicht verfügbar")

    if not dd.get("ok") and not dd.get("skipped"):
        issues.append(f"{WARN}  Dokument-Downloads scheinen nicht zugänglich")

    if not issues:
        print(f"  {OK}  Keine Einschränkungen erkannt")
    for issue in issues:
        print(f"  {issue}")

    # --- 5. Empfehlung ---
    print()
    print("6) Sync-Empfehlung")
    html_ok = html_gr010.get("ok", False)
    api_ok = a1.get("ok", False)
    feed_ok = a2.get("ok", False)

    if html_ok and api_ok and feed_ok:
        mz = a1.get("max_zurueck")
        print(f"  {OK}  Vollständiger Betrieb möglich:")
        print(f"       Täglicher Sync:        oparl-bridge-sync sync          (action=1/2/4 + to020 + Vorlagen, letzte {mz or '?'} Jahre)")
        print("       Erst-Sync:             oparl-bridge-sync initial-sync  (+ gr020 + si018 historisch + Details)")
        print("       Sitzungsdetails:       action=4 (primär) + to010 Playwright (Fallback)")
        print("       Tagesordnungspunkte:   to020 httpx (Beschlusstext, Abstimmung, Anlagen)")
    elif html_ok and not api_ok:
        print(f"  {WARN}  Nur Playwright-Scraping:")
        print("       si010/si018/to010/to020 erreichbar — App API nicht verfügbar")
        print("       Kein inkrementeller Sync per Change-Feed")
    elif api_ok and not html_ok:
        print(f"  {WARN}  Nur App API (HTML-Scraping gesperrt):")
        print(f"       Initialer Sync via action=2 (letzte {a1.get('max_zurueck', '?')} Jahre)")
        print("       Ältere Sitzungen nicht abrufbar")
    elif not html_ok and not api_ok:
        print(f"  {FAIL}  Kein Sync möglich — weder HTML-Scraping noch App API zugänglich")
        print("       Login-Credentials oder VPN erforderlich?")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _quiet
    parser = argparse.ArgumentParser(
        description="Probe an ALLRIS instance for available API capabilities."
    )
    parser.add_argument("url", help="ALLRIS base URL, e.g. https://example.de/allris/")
    parser.add_argument("--json", action="store_true", help="Output raw JSON report")
    args = parser.parse_args()

    if args.json:
        _quiet = True  # suppress progress output when JSON is piped

    _log(f"Prüfe {args.url} ...")
    report = asyncio.run(probe(args.url))
    _log("")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        render_report(report)


if __name__ == "__main__":
    main()
