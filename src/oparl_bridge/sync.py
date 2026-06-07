"""Orchestrates scraping and persisting data to SQLite."""

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from oparl_bridge.db.models import AgendaItem, File, Meeting, Membership, Organization, Paper, Person
from oparl_bridge.db.session import SessionLocal, init_db
from oparl_bridge.scraper import AllrisScraper

logger = logging.getLogger(__name__)


def _eta_clock(seconds: float) -> str:
    from datetime import datetime, timedelta
    t = datetime.now() + timedelta(seconds=seconds)
    return t.strftime('%H:%M')


def _eta_initial(total: int, delay_ms: int, per_item_s: float = 3.5) -> str:
    """Initial ETA estimate using realistic per-item time (delay + page load)."""
    secs = total * (delay_ms / 1000 + per_item_s)
    return f"fertig ca. {_eta_clock(secs)}"


def _progress(i: int, total: int, start: float) -> str:
    elapsed = time.monotonic() - start
    per_item = elapsed / i
    remaining = per_item * (total - i)
    rate = 60 / per_item
    pct = 100 * i // total
    return f"{i}/{total} ({pct}%)  {rate:.0f}/min  fertig ca. {_eta_clock(remaining)}"


async def sync_organizations(scraper: AllrisScraper, db: Session) -> list[Organization]:
    logger.info("Scraping organizations from gr010 ...")
    scraped = await scraper.scrape_organizations()
    orgs = []
    for s in scraped:
        org = db.get(Organization, s.id)
        if org is None:
            org = Organization(id=s.id)
            db.add(org)
        org.name = s.name
        org.short_name = s.short_name
        org.organization_type = s.organization_type
        org.scraped_at = datetime.utcnow()
        # Store next meeting date for display even when SILFDNR not yet assigned
        if s.next_date:
            try:
                from datetime import datetime as dt
                org.next_meeting_date = dt.strptime(s.next_date, "%d.%m.%Y").strftime("%Y-%m-%d")
            except ValueError:
                org.next_meeting_date = None
        else:
            org.next_meeting_date = None
        orgs.append(org)
    db.commit()
    logger.info("Synced %d organizations", len(orgs))

    # Seed stub meetings from gr010 Letzte/Nächste Sitzung columns.
    # These capture meetings (especially future ones) that si018 may not list
    # because they have no SILFDNR link yet.
    from datetime import datetime as dt
    stubs_added = 0
    for s in scraped:
        for silfdnr, date_str in [(s.last_silfdnr, s.last_date), (s.next_silfdnr, s.next_date)]:
            if silfdnr is None or db.get(Meeting, silfdnr) is not None:
                continue
            mtg = Meeting(id=silfdnr, name="")
            mtg.organization_id = s.id
            if date_str:
                try:
                    mtg.start = dt.strptime(date_str, "%d.%m.%Y")
                except ValueError:
                    pass
            mtg.detail_scraped_at = None
            mtg.scraped_at = datetime.utcnow()
            db.add(mtg)
            stubs_added += 1
    if stubs_added:
        db.commit()
        logger.info("Seeded %d stub meeting(s) from gr010", stubs_added)

    return orgs


async def sync_meetings(
    scraper: AllrisScraper, db: Session, organization_id: int | None = None
) -> list[Meeting]:
    import json

    if organization_id is not None:
        logger.info("Scraping meetings for organization %d ...", organization_id)
        scraped = await scraper.scrape_meetings_for_organization(organization_id)
    else:
        logger.info("Scraping all meetings from si010 ...")
        scraped = await scraper.scrape_all_meetings()

    from datetime import datetime as dt
    now_iso = dt.utcnow().isoformat()

    # Split: meetings with SILFDNR vs. planned meetings without ID
    with_id = [s for s in scraped if s.id is not None]
    without_id = [s for s in scraped if s.id is None]

    meetings = []
    for s in with_id:
        is_new = db.get(Meeting, s.id) is None
        mtg = db.get(Meeting, s.id) or Meeting(id=s.id)
        if is_new:
            db.add(mtg)
        mtg.name = s.name
        mtg.organization_id = s.organization_id
        if s.start:
            try:
                mtg.start = dt.fromisoformat(s.start)
            except ValueError:
                pass
        if is_new:
            mtg.detail_scraped_at = None
        mtg.scraped_at = datetime.utcnow()
        meetings.append(mtg)

    # Store future no-ID meetings as JSON on the org (si018 only, needs org_id)
    if organization_id is not None:
        org = db.get(Organization, organization_id)
        if org is not None:
            future_dates = sorted(
                [m.start for m in without_id if m.start and m.start > now_iso]
            )
            org.future_meeting_dates = json.dumps(future_dates) if future_dates else None
            # Keep next_meeting_date in sync with first future entry
            org.next_meeting_date = future_dates[0][:10] if future_dates else org.next_meeting_date

    db.commit()
    logger.info("Synced %d meetings (%d planned without ID)", len(meetings), len(without_id))
    return meetings


async def sync_meeting_details(scraper: AllrisScraper, db: Session) -> None:
    """Scrape to010 detail pages for meetings not yet detail-scraped."""
    pending = db.query(Meeting).filter(Meeting.detail_scraped_at.is_(None)).all()
    if not pending:
        logger.info("All meeting details already up to date.")
        return
    logger.info(
        "Scraping details for %d meeting(s) (%s) ...",
        len(pending),
        _eta_initial(len(pending), scraper.cfg.scraper_delay_ms),
    )
    start = time.monotonic()
    for i, mtg in enumerate(pending, 1):
        try:
            scraped, items = await scraper.scrape_meeting_detail(mtg.id)
            if scraped.location:
                mtg.location = scraped.location
            if scraped.name:
                mtg.name = scraped.name
            if scraped.start and mtg.start is None:
                try:
                    mtg.start = dt.fromisoformat(scraped.start)
                except ValueError:
                    pass
            if scraped.organization_id and mtg.organization_id is None:
                mtg.organization_id = scraped.organization_id
            # Meeting-level documents (Bekanntmachung, Protokoll, etc.)
            db.query(File).filter(File.meeting_id == mtg.id).delete()
            for sf in scraped.files:
                db.add(File(
                    meeting_id=mtg.id,
                    name=sf.name,
                    access_url=sf.url,
                    mime_type="application/pdf",
                    scraped_at=datetime.utcnow(),
                ))
            for item in items:
                is_new_item = db.get(AgendaItem, item.id) is None
                ai = db.get(AgendaItem, item.id) or AgendaItem(id=item.id)
                if is_new_item:
                    db.add(ai)
                ai.meeting_id = mtg.id
                ai.name = item.name
                ai.number = item.number
                ai.public = item.public
                ai.paper_id = item.paper_id
                ai.paper_reference = item.paper_reference
                # result/resolution_text/vote_text are owned by the to020 scraper
                if is_new_item:
                    ai.result_scraped_at = None
                ai.scraped_at = datetime.utcnow()
                for sf in item.files:
                    existing = db.query(File).filter_by(
                        agenda_item_id=ai.id, access_url=sf.url
                    ).first()
                    if existing is None:
                        db.add(File(
                            agenda_item_id=ai.id,
                            name=sf.name,
                            access_url=sf.url,
                            mime_type="application/pdf",
                            scraped_at=datetime.utcnow(),
                        ))
            mtg.detail_scraped_at = datetime.utcnow()
        except Exception as exc:
            logger.warning("Failed to scrape detail for meeting %d: %s", mtg.id, exc)
        if i % 10 == 0 or i == len(pending):
            db.commit()
            logger.info("  %s", _progress(i, len(pending), start))
    logger.info("Meeting details synced.")


async def sync_agenda_item_details(scraper: AllrisScraper, db: Session) -> None:
    """Scrape to020 for all AgendaItems not yet result-scraped.

    Groups items by meeting_id and visits to010 first per group — to020 requires
    a prior to010 visit to establish Wicket navigation state.
    """
    from collections import defaultdict

    from oparl_bridge.scraper.base import _derive_result

    pending = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).all()
    if not pending:
        logger.info("All agenda item details already up to date.")
        return
    logger.info(
        "Scraping to020 for %d agenda item(s) (%s) ...",
        len(pending),
        _eta_initial(len(pending), scraper.cfg.scraper_delay_ms),
    )
    start = time.monotonic()

    # Group by meeting_id so we visit to010 once per meeting before its to020 items
    by_meeting: dict[int | None, list[AgendaItem]] = defaultdict(list)
    for ai in pending:
        by_meeting[ai.meeting_id].append(ai)

    i = 0
    for meeting_id, items in by_meeting.items():
        # Establish Wicket session for this meeting's to020 pages
        if meeting_id is not None:
            try:
                await scraper.prepare_for_to020(meeting_id)
            except Exception as exc:
                logger.warning("prepare_for_to020 failed for meeting %s: %s", meeting_id, exc)

        for ai in items:
            i += 1
            # nichtöffentlich items are always auth-gated — skip the request
            if (ai.number or "").startswith("N") or "nichtöffentlich" in (ai.name or "").lower():
                ai.result_scraped_at = datetime.utcnow()
                if i % 50 == 0 or i == len(pending):
                    db.commit()
                    logger.info("  %s", _progress(i, len(pending), start))
                continue
            try:
                beschlussart, resolution_text, vote_text, word_contribution, item_files = (
                    await scraper.scrape_agenda_item_detail(ai.id)
                )
                ai.result = _derive_result(beschlussart, vote_text) or _derive_result(
                    resolution_text, vote_text
                )
                ai.resolution_text = resolution_text
                ai.vote_text = vote_text
                ai.word_contribution = word_contribution
                # Anlagen from to020 (sentinel URLs) — replace existing ones
                db.query(File).filter(
                    File.agenda_item_id == ai.id,
                    File.access_url.like("allris://to020/%"),
                ).delete()
                for sf in item_files:
                    db.add(File(
                        agenda_item_id=ai.id,
                        name=sf.name,
                        access_url=sf.url,
                        mime_type="application/pdf",
                        scraped_at=datetime.utcnow(),
                    ))
                ai.result_scraped_at = datetime.utcnow()
            except Exception as exc:
                logger.warning("Failed to scrape agenda item detail %d: %s", ai.id, exc)
            if i % 50 == 0 or i == len(pending):
                db.commit()
                logger.info("  %s", _progress(i, len(pending), start))
    logger.info("Agenda item details synced.")


async def sync_papers(scraper: AllrisScraper, db: Session) -> None:
    """Scrape vo020 pages for all AgendaItems that reference a paper not yet stored."""
    # Build map: paper_id → first seen paper_reference (from agenda items)
    ref_map: dict[int, str | None] = {}
    for row in (
        db.query(AgendaItem.paper_id, AgendaItem.paper_reference)
        .filter(AgendaItem.paper_id.isnot(None))
        .distinct()
        .all()
    ):
        pid, pref = row
        if pid not in ref_map:
            ref_map[pid] = pref

    pending_ids = [pid for pid in ref_map if db.get(Paper, pid) is None]
    if not pending_ids:
        logger.info("All papers already synced.")
        return
    logger.info(
        "Scraping %d paper(s) (%s) ...",
        len(pending_ids),
        _eta_initial(len(pending_ids), scraper.cfg.scraper_delay_ms),
    )
    start = time.monotonic()
    for i, paper_id in enumerate(pending_ids, 1):
        try:
            scraped = await scraper.scrape_paper(paper_id)
            if scraped is None:
                continue
            paper = db.get(Paper, paper_id) or Paper(id=paper_id)
            if paper not in db:
                db.add(paper)
            paper.name = scraped.name
            # Use reference from vo020 if found, otherwise fall back to agenda item text
            paper.reference = scraped.reference or ref_map.get(paper_id)
            paper.paper_type = scraped.paper_type
            paper.scraped_at = datetime.utcnow()
            for sf in scraped.files:
                existing = db.query(File).filter_by(paper_id=paper_id, access_url=sf.url).first()
                if existing is None:
                    db.add(File(
                        paper_id=paper_id,
                        name=sf.name,
                        access_url=sf.url,
                        mime_type="application/pdf",
                        scraped_at=datetime.utcnow(),
                    ))
        except Exception as exc:
            logger.warning("Failed to scrape paper %d: %s", paper_id, exc)
        if i % 25 == 0 or i == len(pending_ids):
            db.commit()
            logger.info("  %s", _progress(i, len(pending_ids), start))
    logger.info("Papers synced.")


async def sync_memberships(
    scraper: AllrisScraper, db: Session, orgs: list[Organization]
) -> None:
    """Scrape gr020 for each organization and persist Person + Membership records."""
    logger.info("Scraping memberships for %d organization(s) ...", len(orgs))
    total = 0
    for org in orgs:
        try:
            scraped = await scraper.scrape_memberships(org.id)
        except Exception as exc:
            logger.warning("Failed to scrape memberships for org %d: %s", org.id, exc)
            continue
        # Replace memberships for this org on each sync
        db.query(Membership).filter(Membership.organization_id == org.id).delete()
        for s in scraped:
            person = db.get(Person, s.person_id)
            if person is None:
                person = Person(id=s.person_id, name=s.person_name)
                db.add(person)
            else:
                person.name = s.person_name
            person.scraped_at = datetime.utcnow()
            db.add(Membership(person_id=s.person_id, organization_id=org.id, role=s.role))
            total += 1
        db.commit()
        logger.info("  org %d: %d member(s)", org.id, len(scraped))
    logger.info("Memberships synced: %d total", total)


async def run_full_sync() -> None:
    """Full sync: organizations → meetings → details → papers → item results."""
    init_db()
    scraper = AllrisScraper()
    async with scraper.session():
        with SessionLocal() as db:
            orgs = await sync_organizations(scraper, db)
            for org in orgs:
                try:
                    await sync_meetings(scraper, db, organization_id=org.id)
                except Exception as exc:
                    logger.warning("Failed to sync meetings for org %d: %s", org.id, exc)
            await sync_memberships(scraper, db, orgs)
            await sync_meeting_details(scraper, db)
            await sync_papers(scraper, db)
            await sync_agenda_item_details(scraper, db)
    logger.info("Full sync complete.")
