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
    result: str | None = None           # ACCEPTED / REJECTED / DEFERRED / NODECISION
    resolution_text: str | None = None  # raw Beschlusstext
    vote_text: str | None = None        # raw Abstimmungsergebnis
    files: list["ScrapedFile"] = field(default_factory=list)  # Anlagen + Wortbeiträge


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

    async def warmup(self) -> None:
        """Visit gr010 to establish a Wicket session without parsing anything."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/gr010"))
        finally:
            await page.close()

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
            if source_page == "meeting":
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

            await page.route(f"**/{filename}", handle_route)

            link = await page.query_selector(f'a[href*="{filename}"]')
            if link is None:
                return None
            await link.click()

            status, body = await asyncio.wait_for(captured, timeout=15)
            return body if status == 200 else None
        finally:
            await page.close()

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
      cells[0]: +/- expand button (Wicket Ajax link)
      cells[1]: TOP number ("Ö 1", "N 2") — "N" prefix = nichtöffentlich
      cells[2]: name with to020?TOLFDNR= link
      cells[3]: empty
      cells[4]: Vorlage reference — vo020?VOLFDNR= link (or to010 for Protokolle)
      cells[5]: Beschlussart

    Clicking the expand button reveals Beschlusstext, Abstimmungsergebnis, and
    any attachments (Anlagen, Wortbeiträge) in a sibling row inserted by Wicket.
    """
    base_url = page.url.split("/allris/")[0]

    # Step 1: Collect TOP row handles and click all expand buttons.
    # We keep the handles because DOM mutations (new rows) don't invalidate them.
    all_rows = await page.query_selector_all("table tr")
    top_row_handles = []
    for row in all_rows:
        link = await row.query_selector("td:nth-child(3) a[href*='to020']")
        if link is None:
            continue
        top_row_handles.append(row)
        expand_btn = await row.query_selector("td:first-child a")
        if expand_btn:
            try:
                await expand_btn.click()
                await page.wait_for_timeout(250)
            except Exception:
                pass

    if top_row_handles:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    # Step 2: Parse each TOP row and its immediately following detail row.
    results: list[ScrapedAgendaItem] = []
    for row in top_row_handles:
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
        public = not number_text.upper().startswith("N ")

        paper_id = None
        paper_reference = None
        if len(cells) > 4:
            vo_link = await cells[4].query_selector("a[href*='vo020']")
            if vo_link:
                vo_href = await vo_link.get_attribute("href") or ""
                paper_id = _extract_int_param(vo_href, "VOLFDNR")
                paper_reference = (await vo_link.inner_text()).strip() or None

        # Parse the detail row injected by Wicket after the expand click.
        resolution_text = None
        vote_text = None
        files: list[ScrapedFile] = []
        detail_handle = await row.evaluate_handle("el => el.nextElementSibling")
        detail_elem = detail_handle.as_element()
        if detail_elem:
            # Only treat it as a detail row if it has no TOP link of its own.
            sibling_top_link = await detail_elem.query_selector("a[href*='to020']")
            if sibling_top_link is None:
                resolution_text, vote_text, files = await _parse_detail_content(
                    detail_elem, base_url
                )

        results.append(
            ScrapedAgendaItem(
                id=tolfdnr,
                name=item_name,
                number=number,
                public=public,
                paper_id=paper_id,
                paper_reference=paper_reference,
                resolution_text=resolution_text,
                vote_text=vote_text,
                result=_derive_result(vote_text, resolution_text),
                files=files,
            )
        )
    return results


async def _parse_detail_content(
    elem, base_url: str
) -> tuple[str | None, str | None, list[ScrapedFile]]:
    """Parse the Wicket-injected detail panel of an expanded TOP row."""
    resolution_text = None
    vote_text = None
    files: list[ScrapedFile] = []

    # Try table-based label/value pairs first (most common ALLRIS layout).
    rows = await elem.query_selector_all("tr")
    for row in rows:
        cells = await row.query_selector_all("td, th")
        if len(cells) >= 2:
            label = (await cells[0].inner_text()).strip().lower().rstrip(":")
            value = " ".join((await cells[1].inner_text()).split())
            if "beschluss" in label and "art" not in label and "datum" not in label:
                resolution_text = value or None
            elif "abstimmung" in label:
                vote_text = value or None

    # Fallback: dt/dd pairs.
    if not resolution_text and not vote_text:
        dts = await elem.query_selector_all("dt")
        for dt in dts:
            label = (await dt.inner_text()).strip().lower().rstrip(":")
            sibling = (await dt.evaluate_handle("el => el.nextElementSibling")).as_element()
            if sibling:
                value = " ".join((await sibling.inner_text()).split())
                if "beschluss" in label and "art" not in label:
                    resolution_text = value or None
                elif "abstimmung" in label:
                    vote_text = value or None

    # PDF links (Anlagen, Wortbeiträge).
    seen: set[str] = set()
    for pdf_link in await elem.query_selector_all("a[href*='.pdf']"):
        href = await pdf_link.get_attribute("href") or ""
        if not href:
            continue
        abs_url = href if href.startswith("http") else f"{base_url}/allris/{href.lstrip('/')}"
        if abs_url in seen:
            continue
        seen.add(abs_url)
        link_name = " ".join((await pdf_link.inner_text()).split()) or abs_url.rsplit("/", 1)[-1]
        files.append(ScrapedFile(name=link_name, url=abs_url))

    return resolution_text, vote_text, files


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
