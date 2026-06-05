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


@dataclass
class ScrapedPaper:
    id: int  # VOLFDNR
    name: str
    reference: str | None = None
    paper_type: str | None = None
    file_urls: list[str] = field(default_factory=list)


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
                await self._context.close()
                await self._browser.close()
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
        """Scrape a meeting detail page (si020) including agenda items."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/si020?SILFDNR={meeting_id}"))
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
    """Parse the header block of a si020 meeting detail page."""
    name = ""
    start_str = None
    location = None

    # Try to find meeting title in heading tags
    for selector in ["h1", "h2", ".sitzung-title", "#sitzungstitel"]:
        el = await page.query_selector(selector)
        if el:
            text = (await el.inner_text()).strip()
            if text:
                name = text
                break

    # Fallback: grab the page title
    if not name:
        name = (await page.title()).strip()

    # Look for date/time and location in definition lists or tables
    dts = await page.query_selector_all("dt, th")
    for dt in dts:
        label = (await dt.inner_text()).strip().lower()
        sibling = await dt.evaluate_handle(
            "(el) => el.nextElementSibling"
        )
        if sibling:
            elem = sibling.as_element()
            value = (await elem.inner_text()).strip() if elem else ""
            if "datum" in label or "termin" in label:
                start_str = _parse_german_datetime(value)
            elif "ort" in label or "raum" in label:
                location = value or None

    return ScrapedMeeting(id=meeting_id, name=name, start=start_str, location=location)


async def _parse_agenda_items(page: Page) -> list[ScrapedAgendaItem]:
    """
    Parse Tagesordnungspunkte from a si020 page.

    Typical structure:
      <tr>
        <td>1.</td>
        <td><a href="to020?TOLFDNR=456">Bürgeranfragen</a></td>
        <td>öffentlich</td>
      </tr>
    """
    results: list[ScrapedAgendaItem] = []

    rows = await page.query_selector_all("table tr")
    for row in rows:
        link = await row.query_selector("a[href*='TOLFDNR']")
        if link is None:
            continue

        href = await link.get_attribute("href") or ""
        tolfdnr = _extract_int_param(href, "TOLFDNR")
        if tolfdnr is None:
            continue

        item_name = (await link.inner_text()).strip()
        cells = await row.query_selector_all("td")
        number = None
        public = True

        if len(cells) >= 1:
            number = (await cells[0].inner_text()).strip().rstrip(".") or None
        if len(cells) >= 3:
            visibility = (await cells[2].inner_text()).strip().lower()
            public = "nichtöffentlich" not in visibility and "nicht öffentlich" not in visibility

        # Check for associated Vorlage link
        vo_link = await row.query_selector("a[href*='VOLFDNR']")
        paper_id = None
        if vo_link:
            vo_href = await vo_link.get_attribute("href") or ""
            paper_id = _extract_int_param(vo_href, "VOLFDNR")

        results.append(
            ScrapedAgendaItem(
                id=tolfdnr,
                name=item_name,
                number=number,
                public=public,
                paper_id=paper_id,
            )
        )
    return results


async def _parse_paper(page: Page, paper_id: int) -> ScrapedPaper | None:
    """Parse a vo020 Vorlage page."""
    name = ""
    reference = None
    paper_type = None
    file_urls: list[str] = []

    for selector in ["h1", "h2", ".vorlage-title"]:
        el = await page.query_selector(selector)
        if el:
            text = (await el.inner_text()).strip()
            if text:
                name = text
                break

    if not name:
        name = (await page.title()).strip()

    # Extract metadata from definition lists / tables
    dts = await page.query_selector_all("dt, th")
    for dt in dts:
        label = (await dt.inner_text()).strip().lower()
        sibling_handle = await dt.evaluate_handle("(el) => el.nextElementSibling")
        sibling = sibling_handle.as_element()
        if sibling:
            value = (await sibling.inner_text()).strip()
            if "drucksachen" in label or "vorlagen" in label or "az" in label:
                reference = value or None
            elif "art" in label or "typ" in label:
                paper_type = value or None

    # Collect PDF links
    doc_links = await page.query_selector_all("a[href*='/allris/doc/'], a[href*='doc/']")
    for doc_link in doc_links:
        href = await doc_link.get_attribute("href") or ""
        if href and href not in file_urls:
            # Make absolute
            if href.startswith("http"):
                file_urls.append(href)
            else:
                base = page.url.split("/allris/")[0]
                file_urls.append(f"{base}/allris/{href.lstrip('/')}")

    return ScrapedPaper(
        id=paper_id,
        name=name,
        reference=reference,
        paper_type=paper_type,
        file_urls=file_urls,
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
