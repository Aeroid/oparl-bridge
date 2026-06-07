"""Simple CLI for running scraper tasks."""

import asyncio
import logging
import sys


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: oparl-bridge-sync <command>")
        print("Commands:")
        print("  sync               Full sync of all data")
        print("  sync-orgs          Sync organizations only")
        print("  sync-memberships   Sync committee members (gr020)")
        print("  sync-papers        Sync papers/files for known agenda items")
        print("  sync-item-details  Scrape to020 for Beschluss/Abstimmung per agenda item")
        print("  reset-details      Reset detail_scraped_at (re-scrape all meeting details)")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "sync":
        from oparl_bridge.sync import run_full_sync
        asyncio.run(run_full_sync())
    elif cmd == "sync-item-details":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_agenda_item_details

        async def _run_item_details():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    await sync_agenda_item_details(scraper, db)

        asyncio.run(_run_item_details())
    elif cmd == "sync-papers":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_papers

        async def _run_papers():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    await sync_papers(scraper, db)

        asyncio.run(_run_papers())
    elif cmd == "sync-meeting-details":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_meeting_details

        async def _run_meeting_details():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    await sync_meeting_details(scraper, db)

        asyncio.run(_run_meeting_details())
    elif cmd == "reset-details":
        from oparl_bridge.db.models import Meeting
        from oparl_bridge.db.session import SessionLocal, init_db

        init_db()
        with SessionLocal() as db:
            count = db.query(Meeting).update({Meeting.detail_scraped_at: None})
            db.commit()
            print(f"Reset detail_scraped_at for {count} meeting(s).")
    elif cmd == "sync-memberships":
        from oparl_bridge.db.models import Organization
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_memberships

        async def _run_memberships():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = db.query(Organization).all()
                    await sync_memberships(scraper, db, orgs)

        asyncio.run(_run_memberships())
    elif cmd == "sync-org-meetings":
        from oparl_bridge.db.models import Organization
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_meetings

        async def _run_org_meetings():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = db.query(Organization).all()
                    for org in orgs:
                        try:
                            await sync_meetings(scraper, db, organization_id=org.id)
                        except Exception as exc:
                            import logging
                            logging.getLogger(__name__).warning("org %d failed: %s", org.id, exc)

        asyncio.run(_run_org_meetings())
    elif cmd == "sync-orgs":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_organizations

        async def _run():
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = await sync_organizations(scraper, db)
                    for o in orgs:
                        print(f"  [{o.id}] {o.name} ({o.short_name}) — {o.organization_type}")

        asyncio.run(_run())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
