"""Orchestrates scraping and persisting data to SQLite."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from oparl_bridge.db.models import AgendaItem, Meeting, Organization
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
        mtg = db.get(Meeting, s.id)
        if mtg is None:
            mtg = Meeting(id=s.id)
            db.add(mtg)
        mtg.name = s.name
        mtg.organization_id = s.organization_id
        mtg.location = s.location
        if s.start:
            from datetime import datetime as dt
            try:
                mtg.start = dt.fromisoformat(s.start)
            except ValueError:
                pass
        mtg.scraped_at = datetime.utcnow()
        meetings.append(mtg)
    db.commit()
    logger.info("Synced %d meetings", len(meetings))
    return meetings


async def sync_meeting_details(scraper: AllrisScraper, db: Session) -> None:
    """Scrape to010 detail pages for all meetings to fill in location."""
    meetings = db.query(Meeting).all()
    logger.info("Scraping details for %d meetings ...", len(meetings))
    for mtg in meetings:
        try:
            scraped, agenda_items = await scraper.scrape_meeting_detail(mtg.id)
            if scraped.location:
                mtg.location = scraped.location
            if scraped.name:
                mtg.name = scraped.name
            for item in agenda_items:
                ai = db.get(AgendaItem, item.id)
                if ai is None:
                    ai = AgendaItem(id=item.id)
                    db.add(ai)
                ai.meeting_id = mtg.id
                ai.name = item.name
                ai.number = item.number
                ai.public = item.public
                ai.scraped_at = datetime.utcnow()
            mtg.scraped_at = datetime.utcnow()
        except Exception as exc:
            logger.warning("Failed to scrape detail for meeting %d: %s", mtg.id, exc)
    db.commit()
    logger.info("Meeting details synced.")


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
    logger.info("Full sync complete.")
