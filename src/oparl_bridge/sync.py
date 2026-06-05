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


async def sync_meetings(scraper: AllrisScraper, db: Session, organization_id: int | None = None) -> list[Meeting]:
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


async def run_full_sync() -> None:
    """Full sync: organizations → meetings per organization → details."""
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
    logger.info("Full sync complete.")
