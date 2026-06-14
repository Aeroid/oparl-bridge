"""Playwright-based scraper for ALLRIS Wicket applications."""

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from oparl_bridge.config import Settings
from oparl_bridge.config import settings as default_settings

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 oparl-bridge/0.1"
)


@dataclass
class ScrapedOrganization:
    id: int  # GRLFDNR
    name: str
    short_name: str | None = None
    organization_type: str | None = None
    last_silfdnr: int | None = None   # SILFDNR from gr010 "Letzte Sitzung" column
    last_date: str | None = None      # DD.MM.YYYY text from gr010
    next_silfdnr: int | None = None   # SILFDNR from gr010 "Nächste Sitzung" column
    next_date: str | None = None      # DD.MM.YYYY text from gr010


@dataclass
class ScrapedMeeting:
    id: int | None  # SILFDNR — None for planned meetings not yet assigned an ID
    name: str
    organization_id: int | None = None
    start: str | None = None  # ISO 8601 string, parsed later
    location: str | None = None
    files: list["ScrapedFile"] = field(default_factory=list)  # Sitzungsdokumente
    docs: list[dict] = field(default_factory=list)  # raw doc metadata for meeting_documents table


@dataclass
class ScrapedAgendaItem:
    id: int  # TOLFDNR
    name: str
    number: str | None = None
    public: bool = True
    paper_id: int | None = None  # VOLFDNR
    paper_reference: str | None = None  # e.g. "VO/26/04523"
    result: str | None = None           # ACCEPTED / REJECTED / DEFERRED / NODECISION
    resolution_text: str | None = None  # raw Beschlusstext
    vote_text: str | None = None        # raw Abstimmungsergebnis
    files: list["ScrapedFile"] = field(default_factory=list)  # Anlagen + Wortbeiträge
    beratung_id: int | None = None      # beratung lfdnr from App API (öffentlich/nichtöffentlich section)
    beschluss_datum: str | None = None  # ISO date of the Beschluss for this meeting
    beschluss_totyp: int | None = None  # raw totyp bitmask from <beschluss>
    protokoll_guid: str | None = None   # guid of Protokollauszug doc (typ=138)
    protokoll_crc: str | None = None    # CRC for change detection


@dataclass
class ScrapedFile:
    name: str
    url: str  # absolute URL
    crc: str | None = None   # CRC32 hex from App API (for change detection)
    guid: str | None = None  # ALLRIS document GUID (DOLFDNR) from App API
    typ: int | None = None   # ALLRIS document type from App API


@dataclass
class ScrapedMembership:
    person_id: int  # KPLFDNR
    person_name: str
    role: str | None = None


@dataclass
class ScrapedPaper:
    id: int  # VOLFDNR
    name: str
    reference: str | None = None
    paper_type: str | None = None
    files: list[ScrapedFile] = field(default_factory=list)


class AllrisScraper:
    """Async scraper for ALLRIS municipal information systems.

    Playwright is used for pages that require Wicket JS interactions (gr010, si010, si018).
    httpx is used for static detail pages (to010, to020, vo020, gr020).
    """

    def __init__(self, cfg: Settings = default_settings) -> None:
        self.cfg = cfg
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._http: httpx.AsyncClient | None = None
        self._http_warmed_up: bool = False
        self._http_to010_visited: bool = False  # vo020 requires prior to010 visit
        self._playwright_warmed_up: bool = False

    @asynccontextmanager
    async def session(self) -> AsyncGenerator["AllrisScraper", None]:
        """Context manager that launches a browser and httpx client, tears both down after use."""
        async with async_playwright() as pw:
            self._browser = await pw.chromium.launch(headless=self.cfg.scraper_headless)
            self._context = await self._browser.new_context(
                user_agent=_UA, accept_downloads=True,
            )
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers={"User-Agent": _UA},
            ) as http:
                self._http = http
                self._http_warmed_up = False
                try:
                    yield self
                finally:
                    self._http = None
                    # Browser may have already exited (e.g. received SIGINT directly)
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                    self._browser = None
                    self._context = None

    async def _new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError(
                "Scraper must be used inside an `async with scraper.session()` block"
            )
        page = await self._context.new_page()
        page.set_default_timeout(self.cfg.scraper_timeout_ms)
        return page

    def _url(self, path: str) -> str:
        return f"{self.cfg.allris_base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _goto(self, page: Page, url: str, _retries: int = 2) -> None:
        """Navigate to a URL, respecting the configured inter-request delay.

        Retries up to _retries times on transient errors (ALLRIS is occasionally flaky).
        """
        last_exc: Exception | None = None
        for attempt in range(1, _retries + 1):
            if self.cfg.scraper_delay_ms > 0:
                await asyncio.sleep(self.cfg.scraper_delay_ms / 1000)
            try:
                await page.goto(url, wait_until="networkidle")
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _retries:
                    await asyncio.sleep(2.0)
        raise last_exc

    async def _http_get(self, path: str, _retries: int = 2) -> BeautifulSoup:
        """Fetch a detail page via httpx and return a BeautifulSoup.

        Performs a one-time gr010 warmup to establish JSESSIONID before the first request.
        Retries up to _retries times on transient errors (ALLRIS is occasionally flaky).
        """
        if self._http is None:
            raise RuntimeError("Scraper must be used inside an `async with scraper.session()` block")
        if not self._http_warmed_up:
            await asyncio.sleep(self.cfg.scraper_delay_ms / 1000)
            await self._http.get(self._url("/gr010"))
            self._http_warmed_up = True
        url = self._url(path)
        last_exc: Exception | None = None
        for attempt in range(1, _retries + 1):
            if self.cfg.scraper_delay_ms > 0:
                await asyncio.sleep(self.cfg.scraper_delay_ms / 1000)
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "html.parser")
            except Exception as exc:
                last_exc = exc
                if attempt < _retries:
                    await asyncio.sleep(2.0)
        raise last_exc

    async def _ensure_playwright_session(self) -> None:
        """Visit gr010 to establish a Playwright Wicket session if not already done."""
        if self._playwright_warmed_up:
            return
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/gr010"))
            self._playwright_warmed_up = True
        finally:
            await page.close()

    async def warmup(self) -> None:
        """Visit gr010 to establish a Wicket session without parsing anything."""
        await self._ensure_playwright_session()

    async def fetch_file_content(
        self, source_id: int, file_url: str, source_page: str = "paper"
    ) -> bytes | None:
        """Fetch a PDF from ALLRIS using Playwright route interception.

        source_page="paper"   → navigates to vo020?VOLFDNR=source_id
        source_page="meeting" → navigates to to010?SILFDNR=source_id and expands all TOPs
        """
        page = await self._new_page()
        try:
            await page.goto(self._url("/gr010"), wait_until="networkidle")
            if source_page == "to020-anlage":
                # file_url is sentinel "allris://to020/{tolfdnr}/{index}"
                anlage_idx = int(file_url.rsplit("/", 1)[-1])
                await page.goto(
                    self._url(f"/to020?TOLFDNR={source_id}"),
                    wait_until="networkidle",
                )
                seen: set[str] = set()
                unique_links = []
                for a in await page.query_selector_all("a.attlink.pdf"):
                    href = await a.get_attribute("href") or ""
                    if "anlagenHeader" in href and href not in seen:
                        seen.add(href)
                        unique_links.append(a)
                if anlage_idx >= len(unique_links):
                    return None
                async with page.expect_download(timeout=30000) as dl_info:
                    await unique_links[anlage_idx].click()
                dl = await dl_info.value
                path = await dl.path()
                if not path:
                    return None
                with open(path, "rb") as fh:
                    return fh.read()
            elif source_page == "meeting":
                await page.goto(
                    self._url(f"/to010?SILFDNR={source_id}&refresh=false"),
                    wait_until="networkidle",
                )
                # Expand all TOPs so Wicket registers the resource URLs.
                expand_btns = await page.query_selector_all(
                    "table tr td:first-child a"
                )
                for btn in expand_btns:
                    try:
                        await btn.click()
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            else:
                await page.goto(self._url(f"/vo020?VOLFDNR={source_id}"), wait_until="networkidle")

            captured: asyncio.Future = asyncio.get_running_loop().create_future()
            filename = file_url.rsplit("/", 1)[-1]

            async def handle_route(route):
                resp = await route.fetch()
                body = await resp.body()
                if not captured.done():
                    captured.set_result((resp.status, body))
                await route.fulfill(response=resp)

            # PDF links use target="_blank" — route on the context so the
            # popup page's request is also intercepted.
            await self._context.route(f"**/{filename}", handle_route)

            link = await page.query_selector(f'a[href*="{filename}"]')
            if link is None:
                await self._context.unroute(f"**/{filename}", handle_route)
                return None
            await link.click()

            try:
                status, body = await asyncio.wait_for(captured, timeout=15)
                return body if status == 200 else None
            finally:
                await self._context.unroute(f"**/{filename}", handle_route)
        finally:
            await page.close()

    async def scrape_organizations(self) -> list[ScrapedOrganization]:
        """Scrape the committee list from gr010."""
        await self._ensure_playwright_session()
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/gr010"))
            return await _parse_organizations(page)
        finally:
            await page.close()

    async def scrape_memberships(self, organization_id: int) -> list[ScrapedMembership]:
        """Scrape committee members from gr020 via httpx."""
        soup = await self._http_get(f"/gr020?GRLFDNR={organization_id}")
        return _parse_gr020_bs(soup)

    async def scrape_meetings_for_organization(
        self, organization_id: int
    ) -> list[ScrapedMeeting]:
        """Scrape meetings for a specific committee from si018.

        ALLRIS defaults to a ~15-month window; we expand the Zeitraum
        filter to 2000-01-01–2099-12-31 to capture the full history.
        Paginates through all result pages (default 25 rows/page).
        """
        await self._ensure_playwright_session()
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/si018?GRLFDNR={organization_id}"))
            await _set_full_date_range(page)
            all_results = await _parse_meetings(page, organization_id, capture_no_id=True)
            visited: set[int] = {1}

            while True:
                page_links = await page.query_selector_all("a[href*='pageLink']")
                candidates: list[tuple[int, object]] = []
                for link in page_links:
                    txt = (await link.inner_text()).strip()
                    try:
                        num = int(txt)
                        if num not in visited:
                            candidates.append((num, link))
                    except ValueError:
                        pass
                if not candidates:
                    break
                candidates.sort(key=lambda x: x[0])
                next_num, next_link = candidates[0]
                await next_link.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                visited.add(next_num)
                more = await _parse_meetings(page, organization_id, capture_no_id=True)
                all_results.extend(more)

            # Deduplicate: by SILFDNR for linked meetings, by start for no-ID meetings
            seen_ids: set[int] = set()
            seen_starts: set[str] = set()
            unique: list[ScrapedMeeting] = []
            for m in all_results:
                if m.id is not None:
                    if m.id not in seen_ids:
                        seen_ids.add(m.id)
                        unique.append(m)
                else:
                    key = m.start or ""
                    if key and key not in seen_starts:
                        seen_starts.add(key)
                        unique.append(m)
            return unique
        finally:
            await page.close()

    async def scrape_all_meetings(self) -> list[ScrapedMeeting]:
        """Scrape the full meeting calendar from si010."""
        await self._ensure_playwright_session()
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/si010"))
            return await _parse_meetings(page, organization_id=None)
        finally:
            await page.close()

    async def scrape_meeting_detail(
        self, meeting_id: int
    ) -> tuple[ScrapedMeeting, list[ScrapedAgendaItem]]:
        """Scrape a meeting detail page (to010) via httpx."""
        soup = await self._http_get(f"/to010?SILFDNR={meeting_id}&refresh=false")
        self._http_to010_visited = True
        return _parse_meeting_detail_bs(soup, meeting_id, self.cfg.allris_base_url), \
               _parse_agenda_items_bs(soup, self.cfg.allris_base_url)

    async def scrape_paper(self, paper_id: int) -> ScrapedPaper | None:
        """Scrape a Vorlage/Drucksache detail page (vo020).

        Uses httpx when called after scrape_meeting_detail (to010 visit establishes
        Wicket state required by vo020). Falls back to Playwright otherwise.
        """
        if self._http_to010_visited:
            soup = await self._http_get(f"/vo020?VOLFDNR={paper_id}")
            return _parse_paper_bs(soup, paper_id, self.cfg.allris_base_url)
        # Fallback: Playwright (standalone use without prior to010 visit)
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/vo020?VOLFDNR={paper_id}"))
            return _parse_paper_bs(BeautifulSoup(await page.content(), "html.parser"),
                                   paper_id, self.cfg.allris_base_url)
        finally:
            await page.close()

    async def prepare_for_to020(self, meeting_id: int) -> None:
        """Visit to010 to establish Wicket session state required by to020."""
        await self._http_get(f"/to010?SILFDNR={meeting_id}&refresh=false")
        self._http_to010_visited = True

    async def scrape_agenda_item_detail(
        self, item_id: int
    ) -> tuple[str | None, str | None, str | None, str | None, list[ScrapedFile]]:
        """Scrape to020 via httpx. Returns (beschlussart, resolution_text, vote_text, word_contribution, files)."""
        soup = await self._http_get(f"/to020?TOLFDNR={item_id}")
        return _parse_to020_bs(soup, item_id)


# ---------------------------------------------------------------------------
# Page parsers — kept separate from the scraper class for testability
# ---------------------------------------------------------------------------

async def _parse_organizations(page: Page) -> list[ScrapedOrganization]:
    """
    Parse the gr010 committee list.

    ALLRIS renders section-header rows (no GRLFDNR link, empty Mitglieder/Sitzung cells)
    followed by org rows.  Header text (e.g. "Rat", "Ausschuss", "Fraktion/Gruppe",
    "Mitgliedschaften") becomes organization_type for all following orgs.
    """
    results: list[ScrapedOrganization] = []
    current_type: str | None = None

    # Wait for the main content table to appear
    await page.wait_for_selector("table", timeout=15000)

    rows = await page.query_selector_all("table tr")
    for row in rows:
        link = await row.query_selector("a[href*='GRLFDNR']")
        if link is None:
            # Detect category header: has text in cell[0] but cell[1] is empty
            cells = await row.query_selector_all("td")
            if len(cells) >= 2:
                label = (await cells[0].inner_text()).strip()
                second = (await cells[1].inner_text()).strip()
                if label and not second:
                    current_type = label
            continue

        href = await link.get_attribute("href") or ""
        grlfdnr = _extract_int_param(href, "GRLFDNR")
        if grlfdnr is None:
            continue

        name = (await link.inner_text()).strip()

        # gr010 columns: Name | Mitglieder | Letzte Sitzung | Nächste Sitzung
        cells = await row.query_selector_all("td")
        last_silfdnr = last_date = next_silfdnr = next_date = None

        if len(cells) > 2:
            last_date = (await cells[2].inner_text()).strip() or None
            last_link = await cells[2].query_selector("a[href*='SILFDNR']")
            if last_link:
                last_href = await last_link.get_attribute("href") or ""
                last_silfdnr = _extract_int_param(last_href, "SILFDNR")

        if len(cells) > 3:
            next_date = (await cells[3].inner_text()).strip() or None
            next_link = await cells[3].query_selector("a[href*='SILFDNR']")
            if next_link:
                next_href = await next_link.get_attribute("href") or ""
                next_silfdnr = _extract_int_param(next_href, "SILFDNR")

        results.append(ScrapedOrganization(
            id=grlfdnr, name=name,
            organization_type=current_type,
            last_silfdnr=last_silfdnr, last_date=last_date,
            next_silfdnr=next_silfdnr, next_date=next_date,
        ))
    return results


async def _set_full_date_range(page: Page) -> None:
    """Expand the ALLRIS si018 Zeitraum filter to cover all years.

    ALLRIS defaults to a rolling ~15-month window.  We use stable
    aria-label / tooltip-text selectors instead of Wicket-generated IDs,
    which change between browser sessions.

    fill() does not trigger Wicket's change listeners — we set values via
    JS evaluate and dispatch change/blur events explicitly.
    """
    expand = await page.query_selector(
        'a[data-simpletooltip-text="Zeitraum einblenden"]'
    )
    if expand is None:
        return
    await expand.click()
    try:
        await page.wait_for_selector(
            'input[aria-label="Beginn Datum auswählen"]',
            state="visible",
            timeout=10000,
        )
    except Exception:
        return
    await page.evaluate(
        """([begin_val, end_val]) => {
            const b = document.querySelector("input[aria-label='Beginn Datum auswählen']");
            const e = document.querySelector("input[aria-label='Ende Datum auswählen']");
            [b, e].forEach(function(el, i) {
                if (!el) return;
                el.value = i === 0 ? begin_val : end_val;
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur',   {bubbles: true}));
            });
        }""",
        ["2000-01-01", "2099-12-31"],
    )
    await page.wait_for_timeout(300)
    search_btn = await page.query_selector('button[name="searchPanel:search"]')
    if search_btn:
        await search_btn.click()
        await page.wait_for_load_state("networkidle", timeout=30000)


async def _parse_meetings(
    page: Page,
    organization_id: int | None,
    capture_no_id: bool = False,
) -> list[ScrapedMeeting]:
    """
    Parse meeting rows from si010 or si018.

    si018 column structure: Datum | Uhrzeit | Sitzung (link) | Rang
    Date and time are in separate columns and must be combined.
    Location is not available on list pages — scraped from detail pages.

    When capture_no_id=True (si018 only), rows without a SILFDNR link are also
    captured as ScrapedMeeting(id=None, …) — these are planned meetings not yet
    assigned an ID by ALLRIS.
    """
    results: list[ScrapedMeeting] = []

    try:
        await page.wait_for_selector("a[href*='SILFDNR']", timeout=15000)
    except Exception:
        return results  # no meetings on this page

    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        link = await row.query_selector("a[href*='SILFDNR']")

        if link is not None:
            href = await link.get_attribute("href") or ""
            silfdnr = _extract_int_param(href, "SILFDNR")
            if silfdnr is None:
                continue
            name = (await link.inner_text()).strip()
            start_str = None
            if len(cells) >= 2:
                date_raw = (await cells[0].inner_text()).strip()
                time_raw = (await cells[1].inner_text()).strip()
                combined = f"{date_raw} {time_raw}".replace("\n", " ")
                start_str = _parse_german_datetime(combined) or None
            results.append(ScrapedMeeting(
                id=silfdnr, name=name, organization_id=organization_id, start=start_str,
            ))
        elif capture_no_id and len(cells) >= 3:
            # Planned meeting row: no SILFDNR link yet, but date+time+name present
            date_raw = (await cells[0].inner_text()).strip()
            time_raw = (await cells[1].inner_text()).strip()
            name_raw = (await cells[2].inner_text()).strip()
            if not name_raw:
                continue
            combined = f"{date_raw} {time_raw}".replace("\n", " ")
            start_str = _parse_german_datetime(combined)
            if start_str is None:
                continue  # no valid date — header or non-meeting row
            results.append(ScrapedMeeting(
                id=None, name=name_raw, organization_id=organization_id, start=start_str,
            ))

    return results


def _parse_meeting_detail_bs(soup: BeautifulSoup, meeting_id: int, base_url: str) -> ScrapedMeeting:
    """Parse the header block of a to010 meeting detail page.

    to010 dt labels: Betreff, Gremium, Datum, Status, Uhrzeit, Anlass, Raum, Ort
    """
    name = ""
    raum = None
    ort = None
    datum_raw = None
    uhrzeit_raw = None
    organization_id = None

    # Use only dt elements — th elements in the agenda table share the same
    # labels (e.g. "Betreff") and would overwrite the correctly parsed values.
    for dt in soup.find_all("dt"):
        label = dt.get_text().strip().lower().rstrip(":")
        dd = dt.find_next_sibling("dd")
        value = dd.get_text(" ", strip=True) if dd else ""
        if label == "betreff":
            name = value
        elif label == "raum":
            raum = value or None
        elif label == "ort":
            ort = value or None
        elif label == "datum":
            datum_raw = value or None
        elif label == "uhrzeit":
            uhrzeit_raw = value or None
        elif label == "gremium" and dd is not None:
            a = dd.find("a", href=re.compile(r"GRLFDNR", re.I))
            if a:
                organization_id = _extract_int_param(a.get("href", ""), "GRLFDNR")

    location_parts = [p for p in [raum, ort] if p]
    location = ", ".join(location_parts) or None

    if not name:
        title_tag = soup.find("title")
        name = title_tag.get_text().strip() if title_tag else ""

    start = None
    if datum_raw:
        combined = f"{datum_raw} {uhrzeit_raw or ''}".replace("\n", " ").strip()
        start = _parse_german_datetime(combined)

    # Meeting-level documents: wicket/resource links outside any table
    files: list[ScrapedFile] = []
    seen_urls: set[str] = set()
    allris_base = base_url.rstrip("/")
    for a in soup.find_all("a", href=re.compile(r"wicket/resource")):
        if a.find_parent("table"):
            continue
        href = a.get("href", "")
        label = a.get_text(strip=True)
        if not href.startswith("http"):
            href = f"{allris_base}/allris/{href.lstrip('/')}"
        if href and label and href not in seen_urls:
            seen_urls.add(href)
            files.append(ScrapedFile(name=label, url=href))

    return ScrapedMeeting(id=meeting_id, name=name, location=location, start=start,
                          organization_id=organization_id, files=files)


def _parse_agenda_items_bs(soup: BeautifulSoup, base_url: str) -> list[ScrapedAgendaItem]:
    """Parse Tagesordnungspunkte from a to010 page.

    to010 column structure:
      cells[0]: +/- expand button (ignored — detail data comes from to020)
      cells[1]: TOP number — Wicket anchor id="link_{TOLFDNR}"
      cells[2]: name
      cells[3]: empty
      cells[4]: Vorlage — vo020?VOLFDNR= link
    """
    results: list[ScrapedAgendaItem] = []

    for row in soup.find_all("tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        nr_link = tds[1].find("a", id=re.compile(r"^link_\d+$"))
        if nr_link is None:
            continue

        m_id = re.search(r"link_(\d+)$", nr_link.get("id", ""))
        if m_id is None:
            continue
        tolfdnr = int(m_id.group(1))

        raw_number = tds[1].get_text(strip=True)
        m = re.match(r"^([A-ZÄÖÜa-züäö]*\s*\d+(?:\.\d+)?)", raw_number)
        number = (m.group(1).strip() if m else raw_number.split("\n")[0].strip()) or None
        public = not raw_number.upper().startswith("N ")

        item_name = ""
        if len(tds) > 2:
            name_a = tds[2].find("a")
            item_name = name_a.get_text(strip=True) if name_a else tds[2].get_text(strip=True).split("\n")[0]

        paper_id = None
        paper_reference = None
        if len(tds) > 4:
            vo_link = tds[4].find("a", href=re.compile(r"vo020", re.I))
            if vo_link:
                paper_id = _extract_int_param(vo_link.get("href", ""), "VOLFDNR")
                paper_reference = vo_link.get_text(strip=True) or None

        results.append(ScrapedAgendaItem(
            id=tolfdnr, name=item_name, number=number, public=public,
            paper_id=paper_id, paper_reference=paper_reference,
        ))
    return results


def _derive_result(vote_text: str | None, resolution_text: str | None) -> str | None:
    """Derive an OParl result enum from raw German vote/resolution text."""
    combined = f"{vote_text or ''} {resolution_text or ''}".lower()
    if not combined.strip():
        return None
    if any(w in combined for w in ["abgelehnt", "abgewiesen"]):
        return "REJECTED"
    if any(w in combined for w in ["vertagt", "zurückgestellt", "verschoben"]):
        return "DEFERRED"
    if any(w in combined for w in ["beschlossen", "angenommen", "einstimmig"]):
        return "ACCEPTED"
    nodecision_words = ["zur kenntnis", "kenntnisnahme", "ohne beschluss", "ohne abstimmung"]
    if any(w in combined for w in nodecision_words):
        return "NODECISION"
    return None


def _table_to_md(table: Tag) -> str:
    """Convert an HTML table to a Markdown pipe table."""
    def cell_text(cell: Tag) -> str:
        return " ".join(cell.get_text().split()).replace("|", "\\|")

    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [cell_text(td) for td in tr.find_all(("th", "td"))]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join("---" for _ in rows[0]) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines) + "\n\n"


def _elem_to_text(elem: Tag) -> str:
    """Convert a BS4 element to Markdown, preserving inline and block formatting.

    Headings are stored with their raw levels (h1→#, h2→## …).
    Use shift_headings() at render time to adjust levels for the embedding context.
    """
    def walk(node) -> str:
        if isinstance(node, NavigableString):
            return re.sub(r"[ \t]+", " ", str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name == "br":
            return "\n"
        if name in ("b", "strong"):
            inner = "".join(walk(c) for c in node.children).strip()
            return f"**{inner}**" if inner else ""
        if name in ("em", "i"):
            inner = "".join(walk(c) for c in node.children).strip()
            return f"*{inner}*" if inner else ""
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            inner = "".join(walk(c) for c in node.children).strip()
            return f"{'#' * int(name[1])} {inner}\n\n" if inner else ""
        if name == "a":
            href = (node.get("href") or "").strip()
            inner = "".join(walk(c) for c in node.children).strip()
            if href and not href.startswith(("#", "javascript:")):
                return f"[{inner}]({href})" if inner else href
            return inner
        if name == "ul":
            items = []
            for child in node.children:
                if isinstance(child, Tag) and child.name == "li":
                    items.append("- " + "".join(walk(c) for c in child.children).strip())
            return "\n".join(items) + "\n\n" if items else ""
        if name == "ol":
            items = []
            for child in node.children:
                if isinstance(child, Tag) and child.name == "li":
                    items.append(f"{len(items) + 1}. " + "".join(walk(c) for c in child.children).strip())
            return "\n".join(items) + "\n\n" if items else ""
        if name == "table":
            return _table_to_md(node)
        kids = "".join(walk(c) for c in node.children)
        if name in ("p", "div", "li"):
            t = kids.strip()
            return t + "\n\n" if t else ""
        return kids

    return re.sub(r"\n{3,}", "\n\n", walk(elem)).strip()


def shift_headings(text: str, offset: int) -> str:
    """Shift all Markdown heading levels by offset.

    Use when embedding scraped Markdown into a document that already uses
    certain heading levels, e.g. offset=2 turns # into ### so content
    nests correctly under an existing ## section.
    """
    if not offset or not text:
        return text
    def _bump(m: re.Match) -> str:
        return "#" * min(len(m.group(1)) + offset, 6) + m.group(2)
    return re.sub(r"^(#{1,6})([ \t])", _bump, text, flags=re.MULTILINE)


def _parse_to020_bs(
    soup: BeautifulSoup, tolfdnr: int
) -> tuple[str | None, str | None, str | None, str | None, list[ScrapedFile]]:
    """Parse Beschlussart, Beschlusstext, Abstimmungsergebnis, Wortprotokoll, Anlagen from to020."""
    beschlussart = None
    resolution_text = None
    vote_text = None
    word_contribution = None

    el = soup.find(id="toBeschlussart")
    if el:
        beschlussart = el.get_text(strip=True) or None

    for link in soup.find_all("a", attrs={"data-simpletooltip-text": True}):
        tip = (link.get("data-simpletooltip-text") or "").lower()
        section = link.find_parent(class_="compFull")
        if not section:
            continue
        doc_parts = section.find_all("div", class_="docPart")
        parts = [_elem_to_text(dp) for dp in doc_parts]
        text = re.sub(r"\n {1,}", "\n", "\n\n".join(p for p in parts if p)).strip() or None
        if "abstimmung" in tip:
            vote_text = text
        elif "wortprotokoll" in tip or "wortbeitrag" in tip:
            word_contribution = text
        elif "beschluss" in tip:
            resolution_text = text

    # Capture canonical header-panel links only (attachment-link), skip expandedPanel duplicates (cell-link)
    files: list[ScrapedFile] = []
    seen: set[str] = set()
    anlage_idx = 0
    for a in soup.select("a.attlink.pdf"):
        href = a.get("href", "")
        if "attachment-link" not in href or href in seen:
            continue
        seen.add(href)
        files.append(ScrapedFile(
            name=a.get_text(strip=True),
            url=f"allris://to020/{tolfdnr}/{anlage_idx}",
        ))
        anlage_idx += 1

    return beschlussart, resolution_text, vote_text, word_contribution, files


def _parse_paper_bs(soup: BeautifulSoup, paper_id: int, base_url: str) -> ScrapedPaper:
    """Parse a vo020 Vorlage page."""
    name = ""
    reference = None
    paper_type = None
    files: list[ScrapedFile] = []

    for dt in soup.find_all("dt"):
        label = dt.get_text().strip().lower().rstrip(":")
        dd = dt.find_next_sibling("dd")
        value = " ".join(dd.get_text().split()) if dd else ""
        if label == "betreff":
            name = value
        elif label == "vorlageart":
            paper_type = value or None
        elif label in ("vorlagenzeichen", "vorlagen-nr.", "aktenzeichen", "drucksachennummer"):
            reference = value or None

    if not name:
        title_tag = soup.find("title")
        candidate = title_tag.get_text().strip() if title_tag else ""
        # Ignore generic ALLRIS page titles — they mean the Betreff field was absent
        if candidate and "ratsinformationssystem" not in candidate.lower():
            name = candidate

    # Reference may appear in <h1 class="title"> as "{Vorlageart} - {VO/YY/NNNNN}"
    if reference is None:
        h1 = soup.find("h1", class_="title")
        if h1:
            m = re.search(r"\b(VO/\d{2}/\d+(?:-\d+)?)\b", h1.get_text())
            if m:
                reference = m.group(1)

    seen: set[str] = set()
    allris_base = base_url.rstrip("/")
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        href = a.get("href", "")
        if not href:
            continue
        abs_url = href if href.startswith("http") else f"{allris_base}/allris/{href.lstrip('/')}"
        if abs_url in seen:
            continue
        seen.add(abs_url)
        link_name = " ".join(a.get_text().split()) or href.rsplit("/", 1)[-1]
        files.append(ScrapedFile(name=link_name, url=abs_url))

    return ScrapedPaper(id=paper_id, name=name, reference=reference,
                        paper_type=paper_type, files=files)


def _parse_gr020_bs(soup: BeautifulSoup) -> list[ScrapedMembership]:
    """Parse the gr020 committee detail page for member list."""
    results: list[ScrapedMembership] = []
    for row in soup.find_all("tr"):
        link = row.find("a", href=re.compile(r"KPLFDNR", re.I))
        if link is None:
            continue
        kplfdnr = _extract_int_param(link.get("href", ""), "KPLFDNR")
        if kplfdnr is None:
            continue
        name = link.get_text(strip=True)
        tds = row.find_all("td")
        role = tds[1].get_text(strip=True) or None if len(tds) > 1 else None
        results.append(ScrapedMembership(person_id=kplfdnr, person_name=name, role=role))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_int_param(href: str, param: str) -> int | None:
    """Extract an integer query parameter from a URL fragment like 'si020?SILFDNR=123'."""
    import re
    match = re.search(rf"[?&]{param}=(\d+)", href, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _parse_german_datetime(text: str | None) -> str | None:
    """
    Convert a German date/time string to ISO 8601.

    Handles formats like:
      "12.06.2024 19:00 Uhr"
      "12.06.2024"
    Returns None on failure rather than raising.
    """
    if not text:
        return None
    import re
    from datetime import datetime

    text = text.strip()
    # Try date + time
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", text)
    if m:
        try:
            dt = datetime(
                int(m.group(3)), int(m.group(2)), int(m.group(1)),
                int(m.group(4)), int(m.group(5))
            )
            return dt.isoformat()
        except ValueError:
            pass

    # Try date only
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        try:
            dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return dt.isoformat()
        except ValueError:
            pass

    return None
