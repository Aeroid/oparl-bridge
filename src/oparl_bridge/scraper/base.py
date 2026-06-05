"""Playwright-based scraper for ALLRIS Wicket applications."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from oparl_bridge.config import Settings
from oparl_bridge.config import settings as default_settings


@dataclass
class ScrapedOrganization:
    id: int  # GRLFDNR
    name: str
    short_name: str | None = None
    organization_type: str | None = None


@dataclass
class ScrapedMeeting:
    id: int  # SILFDNR
    name: str
    organization_id: int | None = None
    start: str | None = None  # ISO 8601 string, parsed later
    location: str | None = None


@dataclass
class ScrapedAgendaItem:
    id: int  # TOLFDNR
    name: str
    number: str | None = None
    public: bool = True
    paper_id: int | None = None  # VOLFDNR
    paper_reference: str | None = None  # e.g. "VO/26/04523"


@dataclass
class ScrapedFile:
    name: str
    url: str  # absolute URL


@dataclass
class ScrapedPaper:
    id: int  # VOLFDNR
    name: str
    reference: str | None = None
    paper_type: str | None = None
    files: list[ScrapedFile] = field(default_factory=list)


class AllrisScraper:
    """Async Playwright scraper for ALLRIS municipal information systems."""

    def __init__(self, cfg: Settings = default_settings) -> None:
        self.cfg = cfg
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @asynccontextmanager
    async def session(self) -> AsyncGenerator["AllrisScraper", None]:
        """Context manager that launches a browser and tears it down after use."""
        async with async_playwright() as pw:
            self._browser = await pw.chromium.launch(headless=self.cfg.scraper_headless)
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 oparl-bridge/0.1"
                )
            )
            try:
                yield self
            finally:
                await self._save_cookies()
                await self._context.close()
                await self._browser.close()
                self._browser = None
                self._context = None

    async def _save_cookies(self) -> None:
        """Persist Wicket session cookies so the PDF proxy can reuse them."""
        if self._context is None:
            return
        import json
        from pathlib import Path
        cookies = await self._context.cookies()
        Path("oparl_cookies.json").write_text(json.dumps(cookies))

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

    async def _goto(self, page: Page, url: str) -> None:
        """Navigate to a URL, respecting the configured inter-request delay."""
        if self.cfg.scraper_delay_ms > 0:
            await asyncio.sleep(self.cfg.scraper_delay_ms / 1000)
        await page.goto(url, wait_until="networkidle")

    async def scrape_organizations(self) -> list[ScrapedOrganization]:
        """Scrape the committee list from gr010."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/gr010"))
            return await _parse_organizations(page)
        finally:
            await page.close()

    async def scrape_meetings_for_organization(
        self, organization_id: int
    ) -> list[ScrapedMeeting]:
        """Scrape meetings for a specific committee from si018."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/si018?GRLFDNR={organization_id}"))
            return await _parse_meetings(page, organization_id)
        finally:
            await page.close()

    async def scrape_all_meetings(self) -> list[ScrapedMeeting]:
        """Scrape the full meeting calendar from si010."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/si010"))
            return await _parse_meetings(page, organization_id=None)
        finally:
            await page.close()

    async def scrape_meeting_detail(
        self, meeting_id: int
    ) -> tuple[ScrapedMeeting, list[ScrapedAgendaItem]]:
        """Scrape a meeting detail page (to010) including agenda items."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/to010?SILFDNR={meeting_id}&refresh=false"))
            meeting = await _parse_meeting_detail(page, meeting_id)
            agenda_items = await _parse_agenda_items(page)
            return meeting, agenda_items
        finally:
            await page.close()

    async def scrape_paper(self, paper_id: int) -> ScrapedPaper | None:
        """Scrape a Vorlage/Drucksache detail page (vo020)."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/vo020?VOLFDNR={paper_id}"))
            return await _parse_paper(page, paper_id)
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Page parsers — kept separate from the scraper class for testability
# ---------------------------------------------------------------------------

async def _parse_organizations(page: Page) -> list[ScrapedOrganization]:
    """
    Parse the gr010 committee list.

    ALLRIS renders a table with rows like:
      <tr>
        <td><a href="gr020?GRLFDNR=42">Gemeinderat</a></td>
        <td>GR</td>
        <td>Beschlussgremium</td>
      </tr>
    """
    results: list[ScrapedOrganization] = []

    # Wait for the main content table to appear
    await page.wait_for_selector("table", timeout=15000)

    rows = await page.query_selector_all("table tr")
    for row in rows:
        link = await row.query_selector("a[href*='GRLFDNR']")
        if link is None:
            continue

        href = await link.get_attribute("href") or ""
        grlfdnr = _extract_int_param(href, "GRLFDNR")
        if grlfdnr is None:
            continue

        name = (await link.inner_text()).strip()

        # gr010 columns: Name | Mitglieder | Letzte Sitzung | Nächste Sitzung
        # No short name or organization type available on this page.
        results.append(ScrapedOrganization(id=grlfdnr, name=name))
    return results


async def _parse_meetings(page: Page, organization_id: int | None) -> list[ScrapedMeeting]:
    """
    Parse meeting rows from si010 or si018.

    si018 column structure: Datum | Uhrzeit | Sitzung (link) | Rang
    Date and time are in separate columns and must be combined.
    Location is not available on list pages — scraped from detail pages.
    """
    results: list[ScrapedMeeting] = []

    await page.wait_for_selector("table", timeout=15000)

    rows = await page.query_selector_all("table tr")
    for row in rows:
        link = await row.query_selector("a[href*='SILFDNR']")
        if link is None:
            continue

        href = await link.get_attribute("href") or ""
        silfdnr = _extract_int_param(href, "SILFDNR")
        if silfdnr is None:
            continue

        name = (await link.inner_text()).strip()
        cells = await row.query_selector_all("td")
        start_str = None
        if len(cells) >= 2:
            # cells[0] = date ("Do.,\n24.09.2026"), cells[1] = time ("19:30")
            date_raw = (await cells[0].inner_text()).strip()
            time_raw = (await cells[1].inner_text()).strip()
            combined = f"{date_raw} {time_raw}".replace("\n", " ")
            start_str = _parse_german_datetime(combined) or None

        results.append(
            ScrapedMeeting(
                id=silfdnr,
                name=name,
                organization_id=organization_id,
                start=start_str,
            )
        )
    return results


async def _parse_meeting_detail(page: Page, meeting_id: int) -> ScrapedMeeting:
    """Parse the header block of a to010 meeting detail page.

    to010 dt labels: Betreff, Gremium, Datum, Status, Uhrzeit, Anlass, Raum, Ort
    """
    name = ""
    raum = None
    ort = None

    # Use only dt elements — th elements in the agenda table share the same
    # labels (e.g. "Betreff") and would overwrite the correctly parsed values.
    dts = await page.query_selector_all("dt")
    for dt in dts:
        label = (await dt.inner_text()).strip().lower().rstrip(":")
        sibling = await dt.evaluate_handle("(el) => el.nextElementSibling")
        elem = sibling.as_element()
        value = (await elem.inner_text()).strip() if elem else ""
        if label == "betreff":
            name = value
        elif label == "raum":
            raum = value or None
        elif label == "ort":
            ort = value or None

    # Combine room and address into a single location string
    location_parts = [p for p in [raum, ort] if p]
    location = ", ".join(location_parts) or None

    if not name:
        name = (await page.title()).strip()

    return ScrapedMeeting(id=meeting_id, name=name, location=location)


async def _parse_agenda_items(page: Page) -> list[ScrapedAgendaItem]:
    """
    Parse Tagesordnungspunkte from a to010 page.

    to010 column structure:
      cells[0]: +/- expand button
      cells[1]: TOP number ("Ö 1", "N 2") — "N" prefix = nichtöffentlich
      cells[2]: name with to020?TOLFDNR= link
      cells[3]: empty
      cells[4]: Vorlage reference — vo020?VOLFDNR= link (or to010 for Protokolle)
      cells[5]: Beschlussart
    """
    results: list[ScrapedAgendaItem] = []

    rows = await page.query_selector_all("table tr")
    for row in rows:
        # Name link is in cells[2] (to020?TOLFDNR=...)
        link = await row.query_selector("td:nth-child(3) a[href*='to020']")
        if link is None:
            continue

        href = await link.get_attribute("href") or ""
        tolfdnr = _extract_int_param(href, "TOLFDNR")
        if tolfdnr is None:
            continue

        item_name = (await link.inner_text()).strip()
        cells = await row.query_selector_all("td")

        number_text = (await cells[1].inner_text()).strip() if len(cells) > 1 else ""
        number = number_text or None
        # TOP numbers starting with "N" are nichtöffentlich ("N 1", "N 2", ...)
        public = not number_text.upper().startswith("N ")

        paper_id = None
        paper_reference = None
        if len(cells) > 4:
            vo_link = await cells[4].query_selector("a[href*='vo020']")
            if vo_link:
                vo_href = await vo_link.get_attribute("href") or ""
                paper_id = _extract_int_param(vo_href, "VOLFDNR")
                paper_reference = (await vo_link.inner_text()).strip() or None

        results.append(
            ScrapedAgendaItem(
                id=tolfdnr,
                name=item_name,
                number=number,
                public=public,
                paper_id=paper_id,
                paper_reference=paper_reference,
            )
        )
    return results


async def _parse_paper(page: Page, paper_id: int) -> ScrapedPaper | None:
    """Parse a vo020 Vorlage page."""
    name = ""
    reference = None
    paper_type = None
    files: list[ScrapedFile] = []

    # vo020 dt labels: Betreff, Status, Vorlageart, Federführend, ...
    dts = await page.query_selector_all("dt")
    for dt in dts:
        label = (await dt.inner_text()).strip().lower().rstrip(":")
        sibling = (await dt.evaluate_handle("(el) => el.nextElementSibling")).as_element()
        if sibling:
            value = " ".join((await sibling.inner_text()).split())  # normalise whitespace
            if label == "betreff":
                name = value
            elif label == "vorlageart":
                paper_type = value or None

    if not name:
        name = (await page.title()).strip()

    # PDF links use Wicket resource URLs: /allris/wicket/resource/.../doc<id>.pdf
    seen: set[str] = set()
    base = page.url.split("/allris/")[0]
    for doc_link in await page.query_selector_all("a[href*='.pdf']"):
        href = await doc_link.get_attribute("href") or ""
        if not href:
            continue
        abs_url = href if href.startswith("http") else f"{base}/allris/{href.lstrip('/')}"
        if abs_url in seen:
            continue
        seen.add(abs_url)
        link_name = " ".join((await doc_link.inner_text()).split()) or href.rsplit("/", 1)[-1]
        files.append(ScrapedFile(name=link_name, url=abs_url))

    return ScrapedPaper(
        id=paper_id,
        name=name,
        reference=reference,
        paper_type=paper_type,
        files=files,
    )


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
