"""Simple CLI for running scraper tasks."""

import asyncio
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: oparl-bridge-sync <command>")
        print("Commands:")
        print("  sync          Full sync of all data")
        print("  sync-orgs     Sync organizations only")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "sync":
        from oparl_bridge.sync import run_full_sync
        asyncio.run(run_full_sync())
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
