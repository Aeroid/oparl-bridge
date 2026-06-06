"""Playwright-based scraper for ALLRIS Wicket applications."""

import asyncio
import re
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
    last_silfdnr: int | None = None   # SILFDNR from gr010 "Letzte Sitzung" column
    last_date: str | None = None      # DD.MM.YYYY text from gr010
    next_silfdnr: int | None = None   # SILFDNR from gr010 "Nächste Sitzung" column
    next_date: str | None = None      # DD.MM.YYYY text from gr010


@dataclass
class ScrapedMeeting:
    id: int  # SILFDNR
    name: str
    organization_id: int | None = None
    start: str | None = None  # ISO 8601 string, parsed later
    location: str | None = None
    files: list["ScrapedFile"] = field(default_factory=list)  # Sitzungsdokumente


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
                ),
                accept_downloads=True,
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
        page = await self._new_page()
        try:
            await self._goto(page, self._url("/gr010"))
            return await _parse_organizations(page)
        finally:
            await page.close()

    async def scrape_memberships(self, organization_id: int) -> list[ScrapedMembership]:
        """Scrape committee members from gr020."""
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/gr020?GRLFDNR={organization_id}"))
            return await _parse_gr020(page)
        finally:
            await page.close()

    async def scrape_meetings_for_organization(
        self, organization_id: int
    ) -> list[ScrapedMeeting]:
        """Scrape meetings for a specific committee from si018.

        ALLRIS defaults to a ~15-month window; we expand the Zeitraum
        filter to 2000-01-01–2099-12-31 to capture the full history.
        Paginates through all result pages (default 25 rows/page).
        """
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/si018?GRLFDNR={organization_id}"))
            await _set_full_date_range(page)
            all_results = await _parse_meetings(page, organization_id)
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
                more = await _parse_meetings(page, organization_id)
                all_results.extend(more)

            # Deduplicate by SILFDNR — sidebar links appear on every page
            seen: set[int] = set()
            unique: list[ScrapedMeeting] = []
            for m in all_results:
                if m.id not in seen:
                    seen.add(m.id)
                    unique.append(m)
            return unique
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

    async def scrape_agenda_item_detail(
        self, item_id: int
    ) -> tuple[str | None, str | None, str | None, str | None, list[ScrapedFile]]:
        """Scrape to020 for Beschlussart, Beschlusstext, Abstimmungsergebnis, Wortprotokoll, Anlagen.

        Returns (beschlussart, resolution_text, vote_text, word_contribution, files).
        """
        page = await self._new_page()
        try:
            await self._goto(page, self._url(f"/to020?TOLFDNR={item_id}"))
            return await _parse_to020(page, item_id)
        finally:
            await page.close()


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


async def _parse_meetings(page: Page, organization_id: int | None) -> list[ScrapedMeeting]:
    """
    Parse meeting rows from si010 or si018.

    si018 column structure: Datum | Uhrzeit | Sitzung (link) | Rang
    Date and time are in separate columns and must be combined.
    Location is not available on list pages — scraped from detail pages.
    """
    results: list[ScrapedMeeting] = []

    try:
        await page.wait_for_selector("a[href*='SILFDNR']", timeout=15000)
    except Exception:
        return results  # no meetings on this page

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

    # Meeting-level documents: wicket/resource links outside the agenda table.
    # These are present on initial page load (no Wicket expand needed).
    files: list[ScrapedFile] = []
    seen_urls: set[str] = set()
    for a in await page.query_selector_all("a[href*='wicket/resource']"):
        in_table = await a.evaluate("el => !!el.closest('table')")
        if in_table:
            continue
        href = await a.get_attribute("href") or ""
        label = (await a.inner_text()).strip()
        if href and label and href not in seen_urls:
            seen_urls.add(href)
            files.append(ScrapedFile(name=label, url=href))

    return ScrapedMeeting(id=meeting_id, name=name, location=location, files=files)


async def _parse_agenda_items(page: Page) -> list[ScrapedAgendaItem]:
    """
    Parse Tagesordnungspunkte from a to010 page.

    to010 column structure:
      cells[0]: +/- expand (empty for future meetings)
      cells[1]: TOP number ("Ö 1", "N 2") — Wicket link id="link_{TOLFDNR}"
      cells[2]: name — may have a[href*='to020'] for past meetings, bare <a> for future
      cells[3]: empty
      cells[4]: Vorlage — vo020?VOLFDNR= link (or to010 for Protokolle)
      cells[5]: Beschlussart

    TOLFDNR is reliably available as the numeric suffix of the id attribute on the
    anchor in cells[1]: <a href="#" id="link_1023671">Ö 1</a>.

    Clicking the expand button reveals Beschlusstext, Abstimmungsergebnis, and
    any attachments (Anlagen, Wortbeiträge) in a sibling row inserted by Wicket.
    """
    base_url = page.url.split("/allris/")[0]

    # Step 1: Collect TOP row handles and click all expand buttons.
    # TOP rows are identified by a Wicket anchor with id="link_{TOLFDNR}" in cells[1].
    all_rows = await page.query_selector_all("table tr")
    top_row_handles = []
    for row in all_rows:
        nr_link = await row.query_selector("td:nth-child(2) a[id^='link_']")
        if nr_link is None:
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
        nr_link = await row.query_selector("td:nth-child(2) a[id^='link_']")
        if nr_link is None:
            continue

        link_id = await nr_link.get_attribute("id") or ""
        m_id = re.search(r'link_(\d+)$', link_id)
        if m_id is None:
            continue
        tolfdnr = int(m_id.group(1))

        cells = await row.query_selector_all("td")
        # Item name: prefer to020 link text, fall back to any <a> text in cells[2]
        item_name = ""
        if len(cells) > 2:
            name_link = await cells[2].query_selector("a")
            if name_link:
                item_name = (await name_link.inner_text()).strip()
            if not item_name:
                item_name = (await cells[2].inner_text()).strip().split('\n')[0]

        raw_number = (await cells[1].inner_text()).strip() if len(cells) > 1 else ""
        # Wicket expand may inject "Beschlüsse für …" text directly into the
        # number cell — extract only the leading TOP token (e.g. "Ö 4.1").
        m = re.match(r'^([A-ZÄÖÜa-züäö]*\s*\d+(?:\.\d+)?)', raw_number)
        number_text = m.group(1).strip() if m else raw_number.split('\n')[0].strip()
        number = number_text or None
        public = not raw_number.upper().startswith("N ")

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
            # Only treat it as a detail row if it has no TOP number link of its own.
            sibling_top_link = await detail_elem.query_selector("td:nth-child(2) a[id^='link_']")
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


async def _parse_to020(
    page: Page, tolfdnr: int
) -> tuple[str | None, str | None, str | None, str | None, list[ScrapedFile]]:
    """Parse Beschlussart, Beschlusstext, Abstimmungsergebnis, Wortprotokoll, and Anlagen from to020.

    Returns (beschlussart, resolution_text, vote_text, word_contribution, files).
    """
    beschlussart = None
    resolution_text = None
    vote_text = None
    word_contribution = None

    el = await page.query_selector("#toBeschlussart")
    if el:
        beschlussart = (await el.inner_text()).strip() or None

    _DOM_TO_TEXT = """el => {
        function walk(node) {
            if (node.nodeType === 3) return node.textContent.replace(/[ \\t]+/g, ' ');
            if (!node.tagName) return '';
            const tag = node.tagName.toLowerCase();
            const kids = Array.from(node.childNodes).map(walk).join('');
            if (tag === 'br') return '\\n';
            if (['p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'].includes(tag)) {
                const t = kids.trim();
                return t ? t + '\\n\\n' : '';
            }
            return kids;
        }
        return walk(el).replace(/\\n{3,}/g, '\\n\\n').trim();
    }"""

    for link in await page.query_selector_all("a[data-simpletooltip-text]"):
        tip = (await link.get_attribute("data-simpletooltip-text") or "").lower()
        section = (await link.evaluate_handle("el => el.closest('.compFull')")).as_element()
        if not section:
            continue
        doc_parts = await section.query_selector_all("div.docPart")
        parts = [await dp.evaluate(_DOM_TO_TEXT) for dp in doc_parts]
        import re as _re
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        text = _re.sub(r"\n {1,}", "\n", text).strip() or None
        if "abstimmung" in tip:
            vote_text = text
        elif "wortprotokoll" in tip or "wortbeitrag" in tip:
            word_contribution = text
        elif "beschluss" in tip:
            resolution_text = text

    # Anlagen: unique .attlink.pdf links from the anlagenHeaderPanel
    files: list[ScrapedFile] = []
    seen: set[str] = set()
    anlage_idx = 0
    for a in await page.query_selector_all("a.attlink.pdf"):
        href = await a.get_attribute("href") or ""
        if "anlagenHeader" not in href or href in seen:
            continue
        seen.add(href)
        name = (await a.inner_text()).strip()
        files.append(ScrapedFile(
            name=name,
            url=f"allris://to020/{tolfdnr}/{anlage_idx}",
        ))
        anlage_idx += 1

    return beschlussart, resolution_text, vote_text, word_contribution, files


async def _parse_paper(page: Page, paper_id: int) -> ScrapedPaper | None:
    """Parse a vo020 Vorlage page."""
    name = ""
    reference = None
    paper_type = None
    files: list[ScrapedFile] = []

    # vo020 dt labels: Betreff, Status, Vorlageart, Vorlagenzeichen, Federführend, ...
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
            elif label in ("vorlagenzeichen", "vorlagen-nr.", "aktenzeichen", "drucksachennummer"):
                reference = value or None

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


async def _parse_gr020(page: Page) -> list[ScrapedMembership]:
    """Parse the gr020 committee detail page for member list.

    ALLRIS renders a table with section header rows (empty second cell)
    and member rows: Name (KPLFDNR link) | Art der Mitarbeit (role).
    """
    results: list[ScrapedMembership] = []
    try:
        await page.wait_for_selector("a[href*='KPLFDNR']", timeout=15000)
    except Exception:
        return results

    rows = await page.query_selector_all("table tr")
    for row in rows:
        link = await row.query_selector("a[href*='KPLFDNR']")
        if link is None:
            continue
        href = await link.get_attribute("href") or ""
        kplfdnr = _extract_int_param(href, "KPLFDNR")
        if kplfdnr is None:
            continue
        name = (await link.inner_text()).strip()
        cells = await row.query_selector_all("td")
        role = (await cells[1].inner_text()).strip() or None if len(cells) > 1 else None
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
