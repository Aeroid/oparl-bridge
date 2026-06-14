"""CLI for running oparl-bridge-sync tasks."""

from __future__ import annotations

import asyncio
import logging
import sys


def _plain_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


_HELP = """\
Usage: oparl-bridge-sync <command> [options]

Commands:
  sync               Täglicher Sync: action=1/2/4 + to010-Fallback + to020 + Vorlagen
  initial-sync       Vollständiger Erst-Sync: sync + gr020 Mitglieder + si018 historisch
                     + nochmals to010/to020/Vorlagen für historische Sitzungen
  sync-orgs          Nur Gremien synchronisieren (action=1 + gr010 Playwright)
  sync-memberships   Mitglieder der Gremien abrufen (gr020)
  sync-papers        Vorlagen/Dateien für bekannte Tagesordnungspunkte abrufen (vo020)
  sync-item-details  Beschlüsse und Abstimmungsergebnisse je TOP abrufen (to020)
  reset-details      detail_scraped_at zurücksetzen → erzwingt Neuscraping aller Details

Optionen:
  -h, --help         Diese Hilfe anzeigen
"""


def _print_help() -> None:
    print(_HELP, end="")


def main() -> None:  # noqa: PLR0912, PLR0915
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _print_help()
        sys.exit(0 if len(sys.argv) > 1 else 1)

    cmd = sys.argv[1]

    from oparl_bridge.tui import Dashboard, use_tui

    # ── sync ──────────────────────────────────────────────────────────────────
    if cmd == "sync":
        from oparl_bridge.config import settings
        from oparl_bridge.sync import run_sync

        if not use_tui():
            _plain_logging()
            asyncio.run(run_sync())
            return

        from oparl_bridge.db.models import AgendaItem
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.scraper.app_api import AllrisAppApi
        from oparl_bridge.sync import (
            sync_agenda_item_details,
            sync_meeting_details,
            sync_meetings_from_feed,
            sync_orgs_from_api,
            sync_papers,
        )

        async def _run_sync_tui(dash: Dashboard) -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                async with AllrisAppApi() as app_api:
                    with SessionLocal() as db:
                        dash.set_phase("action=1", "running")
                        await sync_orgs_from_api(app_api, db)
                        info = await app_api.get_system_info()
                        aktiv = sum(1 for g in info.get("gremien", []) if g.get("aktiv"))
                        gesamt = len(info.get("gremien", []))
                        dash.set_phase("action=1", "done", f"{aktiv} aktiv / {gesamt}")

                        if dash.shutdown.is_set():
                            return

                        dash.set_phase("action=2", "running")
                        total_feed, new_stubs = await sync_meetings_from_feed(app_api, db)
                        dash.set_phase("action=2", "done", f"{total_feed} Sitzungen · {new_stubs} neu")

                        if dash.shutdown.is_set():
                            return

                        dash.set_phase("Sitzungsdetails", "running")
                        on_prog = dash.start_task("Sitzungsdetails", total_feed)
                        on_org = dash.start_org_task()
                        await sync_meeting_details(
                            scraper, db, app_api=app_api,
                            shutdown=dash.shutdown, on_progress=on_prog, on_org=on_org,
                        )
                        dash.finish_task()
                        dash.set_phase("Sitzungsdetails", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        dash.set_phase("to020 Beschlüsse", "running")
                        n_items = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).count()
                        on_prog = dash.start_task("Beschlüsse (to020)", n_items)
                        await sync_agenda_item_details(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("to020 Beschlüsse", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        dash.set_phase("vo020 Vorlagen", "running")
                        n_papers = (
                            db.query(AgendaItem.paper_id)
                            .filter(AgendaItem.paper_id.isnot(None))
                            .distinct()
                            .count()
                        )
                        on_prog = dash.start_task("Vorlagen (vo020)", n_papers)
                        await sync_papers(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("vo020 Vorlagen", "done" if not dash.shutdown.is_set() else "skipped")

            logging.getLogger("oparl_bridge").info("sync abgeschlossen.")

        with Dashboard("sync", settings.body_name) as dash:
            dash.add_phase("action=1", "pending", "Gremien")
            dash.add_phase("action=2", "pending", "Sitzungs-Feed")
            dash.add_phase("Sitzungsdetails", "pending", "action=4 + to010")
            dash.add_phase("to020 Beschlüsse", "pending")
            dash.add_phase("vo020 Vorlagen", "pending")
            try:
                asyncio.get_event_loop().run_until_complete(_run_sync_tui(dash))
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    # ── initial-sync ──────────────────────────────────────────────────────────
    elif cmd == "initial-sync":
        from oparl_bridge.config import settings
        from oparl_bridge.sync import run_initial_sync

        if not use_tui():
            _plain_logging()
            asyncio.run(run_initial_sync())
            return

        from oparl_bridge.db.models import AgendaItem, Meeting
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.scraper.app_api import AllrisAppApi
        from oparl_bridge.sync import (
            sync_agenda_item_details,
            sync_meeting_details,
            sync_meetings,
            sync_meetings_from_feed,
            sync_memberships,
            sync_organizations,
            sync_orgs_from_api,
            sync_papers,
        )

        async def _run_initial_tui(dash: Dashboard) -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                async with AllrisAppApi() as app_api:
                    with SessionLocal() as db:
                        # action=1
                        dash.set_phase("action=1", "running")
                        await sync_orgs_from_api(app_api, db)
                        info = await app_api.get_system_info()
                        aktiv = sum(1 for g in info.get("gremien", []) if g.get("aktiv"))
                        gesamt = len(info.get("gremien", []))
                        dash.set_phase("action=1", "done", f"{aktiv} aktiv / {gesamt}")

                        if dash.shutdown.is_set():
                            return

                        # action=2
                        dash.set_phase("action=2", "running")
                        total_feed, new_stubs = await sync_meetings_from_feed(app_api, db)
                        dash.set_phase("action=2", "done", f"{total_feed} Sitzungen · {new_stubs} neu")

                        if dash.shutdown.is_set():
                            return

                        # action=4 + to010
                        dash.set_phase("Sitzungsdetails", "running")
                        on_prog = dash.start_task("Sitzungsdetails", total_feed)
                        on_org = dash.start_org_task()
                        await sync_meeting_details(
                            scraper, db, app_api=app_api,
                            shutdown=dash.shutdown, on_progress=on_prog, on_org=on_org,
                        )
                        dash.finish_task()
                        dash.set_phase("Sitzungsdetails", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # to020
                        dash.set_phase("to020 Beschlüsse", "running")
                        n_items = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).count()
                        on_prog = dash.start_task("Beschlüsse (to020)", n_items)
                        await sync_agenda_item_details(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("to020 Beschlüsse", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # vo020 / Vorlagen
                        dash.set_phase("vo020 Vorlagen", "running")
                        n_papers = (
                            db.query(AgendaItem.paper_id)
                            .filter(AgendaItem.paper_id.isnot(None))
                            .distinct()
                            .count()
                        )
                        on_prog = dash.start_task("Vorlagen (vo020)", n_papers)
                        await sync_papers(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("vo020 Vorlagen", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # gr020 Mitglieder
                        dash.set_phase("gr020 Mitglieder", "running")
                        orgs = await sync_organizations(scraper, db)
                        await sync_memberships(scraper, db, orgs)
                        dash.set_phase("gr020 Mitglieder", "done", f"{len(orgs)} Gremien")

                        if dash.shutdown.is_set():
                            return

                        # si018 historisch
                        dash.set_phase("si018 historisch", "running")
                        for org in orgs:
                            if dash.shutdown.is_set():
                                break
                            try:
                                await sync_meetings(scraper, db, organization_id=org.id)
                            except Exception as exc:
                                logging.getLogger("oparl_bridge").warning("org %d: %s", org.id, exc)
                        dash.set_phase("si018 historisch", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # Historische Sitzungsdetails
                        n_hist = db.query(Meeting).filter(Meeting.detail_scraped_at.is_(None)).count()
                        dash.set_phase("Sitzungsdetails hist.", "running", f"{n_hist} ausstehend")
                        on_prog = dash.start_task("Sitzungsdetails historisch", n_hist)
                        on_org2 = dash.start_org_task()
                        await sync_meeting_details(
                            scraper, db, app_api=app_api,
                            shutdown=dash.shutdown, on_progress=on_prog, on_org=on_org2,
                        )
                        dash.finish_task()
                        dash.set_phase("Sitzungsdetails hist.", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # Historische Beschlüsse
                        n_hist_items = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).count()
                        dash.set_phase("to020 hist. Beschlüsse", "running", f"{n_hist_items} ausstehend")
                        on_prog = dash.start_task("Beschlüsse historisch (to020)", n_hist_items)
                        await sync_agenda_item_details(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("to020 hist. Beschlüsse", "done" if not dash.shutdown.is_set() else "skipped")

                        if dash.shutdown.is_set():
                            return

                        # Historische Vorlagen
                        n_hist_papers = (
                            db.query(AgendaItem.paper_id)
                            .filter(AgendaItem.paper_id.isnot(None))
                            .distinct()
                            .count()
                        )
                        dash.set_phase("vo020 hist. Vorlagen", "running", f"{n_hist_papers} ausstehend")
                        on_prog = dash.start_task("Vorlagen historisch (vo020)", n_hist_papers)
                        await sync_papers(
                            scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                        )
                        dash.finish_task()
                        dash.set_phase("vo020 hist. Vorlagen", "done" if not dash.shutdown.is_set() else "skipped")

            logging.getLogger("oparl_bridge").info("initial-sync abgeschlossen.")

        with Dashboard("initial-sync", settings.body_name) as dash:
            dash.add_phase("action=1", "pending", "Gremien")
            dash.add_phase("action=2", "pending", "Sitzungs-Feed")
            dash.add_phase("Sitzungsdetails", "pending", "action=4 + to010")
            dash.add_phase("to020 Beschlüsse", "pending")
            dash.add_phase("vo020 Vorlagen", "pending")
            dash.add_phase("gr020 Mitglieder", "pending")
            dash.add_phase("si018 historisch", "pending")
            dash.add_phase("Sitzungsdetails hist.", "pending")
            dash.add_phase("to020 hist. Beschlüsse", "pending")
            dash.add_phase("vo020 hist. Vorlagen", "pending")
            try:
                asyncio.get_event_loop().run_until_complete(_run_initial_tui(dash))
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    # ── sync-item-details ─────────────────────────────────────────────────────
    elif cmd == "sync-item-details":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_agenda_item_details

        if not use_tui():
            _plain_logging()

            async def _run() -> None:
                init_db()
                scraper = AllrisScraper()
                async with scraper.session():
                    with SessionLocal() as db:
                        await sync_agenda_item_details(scraper, db)

            asyncio.run(_run())
            return

        from oparl_bridge.config import settings
        from oparl_bridge.db.models import AgendaItem

        async def _run_items_tui(dash: Dashboard) -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    n = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).count()
                    dash.set_phase("to020", "running", f"{n} ausstehend")
                    on_prog = dash.start_task("Beschlüsse (to020)", n)
                    await sync_agenda_item_details(
                        scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                    )
                    dash.finish_task()
                    dash.set_phase("to020", "done")

        with Dashboard("sync-item-details", settings.body_name) as dash:
            dash.add_phase("to020", "pending", "Beschlüsse/Wortbeiträge")
            try:
                asyncio.get_event_loop().run_until_complete(_run_items_tui(dash))
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    # ── sync-papers ───────────────────────────────────────────────────────────
    elif cmd == "sync-papers":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_papers

        if not use_tui():
            _plain_logging()

            async def _run_papers() -> None:
                init_db()
                scraper = AllrisScraper()
                async with scraper.session():
                    with SessionLocal() as db:
                        await sync_papers(scraper, db)

            asyncio.run(_run_papers())
            return

        from oparl_bridge.config import settings
        from oparl_bridge.db.models import AgendaItem

        async def _run_papers_tui(dash: Dashboard) -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    n = (
                        db.query(AgendaItem.paper_id)
                        .filter(AgendaItem.paper_id.isnot(None))
                        .distinct()
                        .count()
                    )
                    dash.set_phase("vo020", "running", f"~{n} ausstehend")
                    on_prog = dash.start_task("Vorlagen (vo020)", n)
                    await sync_papers(
                        scraper, db, shutdown=dash.shutdown, on_progress=on_prog,
                    )
                    dash.finish_task()
                    dash.set_phase("vo020", "done")

        with Dashboard("sync-papers", settings.body_name) as dash:
            dash.add_phase("vo020", "pending", "Vorlagen")
            try:
                asyncio.get_event_loop().run_until_complete(_run_papers_tui(dash))
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    # ── sync-orgs ─────────────────────────────────────────────────────────────
    elif cmd == "sync-orgs":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_organizations

        _plain_logging()

        async def _run() -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = await sync_organizations(scraper, db)
                    for o in orgs:
                        print(f"  [{o.id}] {o.name} ({o.short_name}) — {o.organization_type}")

        asyncio.run(_run())

    # ── sync-memberships ──────────────────────────────────────────────────────
    elif cmd == "sync-memberships":
        from oparl_bridge.db.models import Organization
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_memberships

        _plain_logging()

        async def _run_memberships() -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = db.query(Organization).all()
                    await sync_memberships(scraper, db, orgs)

        asyncio.run(_run_memberships())

    # ── reset-details ─────────────────────────────────────────────────────────
    elif cmd == "reset-details":
        from oparl_bridge.db.models import Meeting
        from oparl_bridge.db.session import SessionLocal, init_db

        init_db()
        with SessionLocal() as db:
            count = db.query(Meeting).update({Meeting.detail_scraped_at: None})
            db.commit()
            print(f"detail_scraped_at zurückgesetzt für {count} Sitzung(en).")

    # ── sync-meeting-details (intern) ─────────────────────────────────────────
    elif cmd == "sync-meeting-details":
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.scraper.app_api import AllrisAppApi
        from oparl_bridge.sync import sync_meeting_details

        _plain_logging()

        async def _run_meeting_details() -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                async with AllrisAppApi() as app_api:
                    with SessionLocal() as db:
                        await sync_meeting_details(scraper, db, app_api=app_api)

        asyncio.run(_run_meeting_details())

    elif cmd == "sync-org-meetings":
        from oparl_bridge.db.models import Organization
        from oparl_bridge.db.session import SessionLocal, init_db
        from oparl_bridge.scraper import AllrisScraper
        from oparl_bridge.sync import sync_meetings

        _plain_logging()

        async def _run_org_meetings() -> None:
            init_db()
            scraper = AllrisScraper()
            async with scraper.session():
                with SessionLocal() as db:
                    orgs = db.query(Organization).all()
                    for org in orgs:
                        try:
                            await sync_meetings(scraper, db, organization_id=org.id)
                        except Exception as exc:
                            logging.getLogger(__name__).warning("org %d: %s", org.id, exc)

        asyncio.run(_run_org_meetings())

    else:
        print(f"Unbekannter Befehl: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
