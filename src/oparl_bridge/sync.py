"""Orchestrates scraping and persisting data to SQLite."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from oparl_bridge.db.models import AgendaItem, File, Meeting, Organization, Paper
from oparl_bridge.db.session import SessionLocal, init_db
from oparl_bridge.scraper import AllrisScraper

logger = logging.getLogger(__name__)


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
        orgs.append(org)
    db.commit()
    logger.info("Synced %d organizations", len(orgs))
    return orgs


async def sync_meetings(
    scraper: AllrisScraper, db: Session, organization_id: int | None = None
) -> list[Meeting]:
    if organization_id is not None:
        logger.info("Scraping meetings for organization %d ...", organization_id)
        scraped = await scraper.scrape_meetings_for_organization(organization_id)
    else:
        logger.info("Scraping all meetings from si010 ...")
        scraped = await scraper.scrape_all_meetings()

    meetings = []
    for s in scraped:
        is_new = db.get(Meeting, s.id) is None
        mtg = db.get(Meeting, s.id) or Meeting(id=s.id)
        if is_new:
            db.add(mtg)
        mtg.name = s.name
        mtg.organization_id = s.organization_id
        if s.start:
            from datetime import datetime as dt
            try:
                mtg.start = dt.fromisoformat(s.start)
            except ValueError:
                pass
        # Reset detail_scraped_at for new meetings so detail scraper picks them up.
        # Never clear it for existing meetings — that would re-scrape unnecessarily.
        if is_new:
            mtg.detail_scraped_at = None
        mtg.scraped_at = datetime.utcnow()
        meetings.append(mtg)
    db.commit()
    logger.info("Synced %d meetings", len(meetings))
    return meetings


async def sync_meeting_details(scraper: AllrisScraper, db: Session) -> None:
    """Scrape to010 detail pages for meetings not yet detail-scraped."""
    pending = db.query(Meeting).filter(Meeting.detail_scraped_at.is_(None)).all()
    if not pending:
        logger.info("All meeting details already up to date.")
        return
    logger.info("Scraping details for %d meetings ...", len(pending))
    for mtg in pending:
        try:
            scraped, items = await scraper.scrape_meeting_detail(mtg.id)
            if scraped.location:
                mtg.location = scraped.location
            if scraped.name:
                mtg.name = scraped.name
            for item in items:
                ai = db.get(AgendaItem, item.id)
                if ai is None:
                    ai = AgendaItem(id=item.id)
                    db.add(ai)
                ai.meeting_id = mtg.id
                ai.name = item.name
                ai.number = item.number
                ai.public = item.public
                ai.paper_id = item.paper_id
                ai.paper_reference = item.paper_reference
                ai.scraped_at = datetime.utcnow()
            mtg.detail_scraped_at = datetime.utcnow()
        except Exception as exc:
            logger.warning("Failed to scrape detail for meeting %d: %s", mtg.id, exc)
    db.commit()
    logger.info("Meeting details synced.")


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
    logger.info("Scraping %d paper(s) ...", len(pending_ids))
    for paper_id in pending_ids:
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
    db.commit()
    logger.info("Papers synced.")


async def run_full_sync() -> None:
    """Full sync: organizations → meetings → meeting details (location, agenda)."""
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
            await sync_meeting_details(scraper, db)
            await sync_papers(scraper, db)
    logger.info("Full sync complete.")
