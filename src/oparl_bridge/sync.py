"""Orchestrates scraping and persisting data to SQLite."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from oparl_bridge.db.models import (
    AgendaItem,
    File,
    Meeting,
    MeetingDocument,
    Membership,
    Organization,
    Paper,
    Person,
)
from oparl_bridge.db.session import SessionLocal, init_db
from oparl_bridge.scraper import AllrisScraper
from oparl_bridge.scraper.app_api import MeetingRestrictedError

if TYPE_CHECKING:
    from oparl_bridge.scraper.app_api import AllrisAppApi

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5


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


async def sync_meeting_details(
    scraper: AllrisScraper | None,
    db: Session,
    app_api: AllrisAppApi | None = None,
    shutdown: asyncio.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_org: Callable[[str, int, int], None] | None = None,
) -> None:
    """Scrape to010 detail pages for meetings not yet detail-scraped.

    If `app_api` is provided, the ALLRIS Windows App XML API (action=4) is tried
    first. Falls back to Playwright/httpx (to010) if `scraper` is given.
    If neither can fetch a meeting, it is skipped and left pending for the next run.
    """
    pending = db.query(Meeting).filter(Meeting.detail_scraped_at.is_(None)).all()
    total_count = db.query(Meeting).count()
    already_done = total_count - len(pending)
    if not pending:
        logger.info("Alle Sitzungsdetails aktuell (%d gesamt).", total_count)
        return
    source = "App API (action=4)" if scraper is None else "App API (action=4) + Playwright (to010)"
    logger.info(
        "Details via %s — %d ausstehend von %d gesamt (%d bereits erledigt) ...",
        source,
        len(pending),
        total_count,
        already_done,
    )
    start = time.monotonic()
    app_api_hits = 0
    restricted_count = 0
    consecutive_errors = 0

    total_orgs_db = db.query(Organization).count() or 1
    seen_org_ids: set[int] = set()

    for i, mtg in enumerate(pending, 1):
        if shutdown is not None and shutdown.is_set():
            db.commit()
            logger.info(
                "Abgebrochen nach %d/%d ausstehenden Sitzungen (%d nichtöffentlich).",
                i - 1, len(pending), restricted_count,
            )
            return

        # Pre-flight log: use meeting date if known, else action=2 modification timestamp
        if mtg.start:
            _pre_date = str(mtg.start)[:10]
        elif mtg.app_api_ts:
            _pre_date = f"~{str(mtg.app_api_ts)[:10]}"  # ~ = modification date, not meeting date
        else:
            _pre_date = None
        _pre_org_lbl: str | None = None
        if mtg.organization_id:
            _pre_org = db.get(Organization, mtg.organization_id)
            _pre_org_lbl = _pre_org.name if _pre_org else f"Org#{mtg.organization_id}"
        _pre_label = " ".join(p for p in [_pre_date, _pre_org_lbl] if p) or f"SILFDNR={mtg.id}"
        logger.debug("▶ %s", _pre_label)

        try:
                scraped = None
                items = []
                used_app_api = False

                if app_api is not None:
                    try:
                        result = await app_api.get_session_xml(mtg.id)
                        if result is not None:
                            scraped, items = result
                            used_app_api = True
                            app_api_hits += 1
                            date_str = (scraped.start or "")[:10]
                            n_pub = sum(1 for it in items if it.public)
                            n_res = sum(1 for it in items if it.result)
                            n_anl = sum(len(it.files) for it in items)
                            _extras = ", ".join(filter(None, [
                                f"{n_res} Beschl." if n_res else None,
                                f"{n_anl} Anl." if n_anl else None,
                            ]))
                            logger.debug("✓ %s — %d TOPs (%d öff.%s)", date_str, len(items), n_pub,
                                         f", {_extras}" if _extras else "")
                    except MeetingRestrictedError:
                        # 302 = nichtöffentlich oder zu alt — als erledigt markieren, kein Fallback
                        logger.debug("✗ nichtöffentlich / zu alt (302)")
                        mtg.detail_scraped_at = datetime.utcnow()
                        restricted_count += 1
                        consecutive_errors = 0
                        if i % 10 == 0 or i == len(pending):
                            db.commit()
                            logger.info(
                                "  %s  · %d nichtöffentlich",
                                _progress(already_done + i, total_count, start), restricted_count,
                            )
                        continue
                    except Exception as exc:
                        logger.debug("App API failed for meeting %d, falling back: %s", mtg.id, exc)

                if scraped is None:
                    if scraper is None:
                        # No Playwright fallback — leave this meeting pending for a later full sync
                        if i % 10 == 0 or i == len(pending):
                            db.commit()
                            logger.info(
                                "  %s  · %d nichtöffentlich",
                                _progress(already_done + i, total_count, start), restricted_count,
                            )
                        continue
                    logger.debug("▶ Playwright to010 Sitzung %d", mtg.id)
                    scraped, items = await scraper.scrape_meeting_detail(mtg.id)
                    date_str = (scraped.start or "")[:10]
                    n_pub_pw = sum(1 for it in items if it.public)
                    n_anl_pw = sum(len(it.files) for it in items)
                    _extras_pw = f", {n_anl_pw} Anl." if n_anl_pw else ""
                    logger.debug("✓ %s — %d TOPs (%d öff.%s, Playwright)",
                                 date_str, len(items), n_pub_pw, _extras_pw)

                if scraped.location:
                    mtg.location = scraped.location
                if scraped.name:
                    mtg.name = scraped.name
                if scraped.start and mtg.start is None:
                    try:
                        mtg.start = datetime.fromisoformat(scraped.start)
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
                        doc_crc=sf.crc,
                        doc_guid=sf.guid,
                        scraped_at=datetime.utcnow(),
                    ))

                # Upsert meeting_documents (full doc index with CRC for change detection)
                for doc in scraped.docs:
                    existing_doc = (
                        db.query(MeetingDocument)
                        .filter_by(meeting_id=mtg.id, guid=doc["guid"])
                        .first()
                    )
                    if existing_doc is None:
                        db.add(MeetingDocument(
                            meeting_id=mtg.id,
                            guid=doc["guid"],
                            typ=doc["typ"],
                            dotimestamp=doc.get("dotimestamp"),
                            crc=doc.get("crc"),
                        ))
                    else:
                        existing_doc.crc = doc.get("crc")
                        existing_doc.dotimestamp = doc.get("dotimestamp")

                # Shortcut GUIDs for Bekanntmachung (108) and öffentliches Protokoll (116)
                for doc in scraped.docs:
                    if doc["typ"] == 108 and not mtg.bekanntmachung_guid:
                        mtg.bekanntmachung_guid = doc["guid"]
                    elif doc["typ"] == 116:
                        mtg.protokoll_guid = doc["guid"]
                        mtg.protokoll_crc = doc.get("crc")

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
                    ai.beratung_id = item.beratung_id
                    # result from App API (totyp bitmask) is more reliable than text parsing;
                    # only override if result_scraped_at is not yet set (to020 hasn't run).
                    if used_app_api and item.result and ai.result_scraped_at is None:
                        ai.result = item.result
                    if used_app_api:
                        if item.beschluss_datum:
                            ai.beschluss_datum = item.beschluss_datum
                        if item.beschluss_totyp is not None:
                            ai.beschluss_totyp = item.beschluss_totyp
                        if item.protokoll_guid:
                            ai.protokoll_guid = item.protokoll_guid
                            ai.protokoll_crc = item.protokoll_crc
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
                                doc_crc=sf.crc,
                                scraped_at=datetime.utcnow(),
                            ))
                mtg.detail_scraped_at = datetime.utcnow()
                consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logger.warning(
                "Failed to scrape detail for meeting %d: %s (%d consecutive)",
                mtg.id, exc, consecutive_errors,
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                db.commit()
                logger.error(
                    "Aborting sync_meeting_details after %d consecutive errors at meeting %d.",
                    consecutive_errors, mtg.id,
                )
                return
        # Org progress bar: advance when we encounter a new org for the first time
        if on_org and mtg.organization_id and mtg.organization_id not in seen_org_ids:
            seen_org_ids.add(mtg.organization_id)
            _org_obj = db.get(Organization, mtg.organization_id)
            _org_name = _org_obj.name if _org_obj else f"Org#{mtg.organization_id}"
            on_org(_org_name, len(seen_org_ids), total_orgs_db)
        if on_progress:
            on_progress(already_done + i, total_count)
        if i % 10 == 0 or i == len(pending):
            db.commit()
            restricted_note = f"  · {restricted_count} nichtöffentlich" if restricted_count else ""
            logger.info("  %s%s", _progress(already_done + i, total_count, start), restricted_note)
    if app_api is not None:
        no_fallback = len(pending) - app_api_hits - restricted_count if scraper is None else 0
        logger.info(
            "Details: %d öffentlich (action=4), %d nichtöffentlich (302 → übersprungen)%s.",
            app_api_hits,
            restricted_count,
            f", {no_fallback} ohne Playwright-Fallback (→ nächster sync)" if no_fallback else "",
        )
    else:
        logger.info("Details abgerufen.")


async def sync_agenda_item_details(
    scraper: AllrisScraper,
    db: Session,
    shutdown: asyncio.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Scrape to020 for all AgendaItems not yet result-scraped.

    Groups items by meeting_id and visits to010 first per group — to020 requires
    a prior to010 visit to establish Wicket navigation state.
    """
    from collections import defaultdict

    from oparl_bridge.scraper.base import _derive_result

    pending = db.query(AgendaItem).filter(AgendaItem.result_scraped_at.is_(None)).all()
    total_items = db.query(AgendaItem).count()
    already_done_items = total_items - len(pending)
    if not pending:
        logger.info("Alle TOP-Details aktuell (%d gesamt).", total_items)
        return
    logger.info(
        "to020 — %d ausstehend von %d gesamt (%d bereits erledigt) ...",
        len(pending),
        total_items,
        already_done_items,
    )
    start = time.monotonic()

    # Group by meeting_id so we visit to010 once per meeting before its to020 items
    by_meeting: dict[int | None, list[AgendaItem]] = defaultdict(list)
    for ai in pending:
        by_meeting[ai.meeting_id].append(ai)

    i = 0
    consecutive_errors = 0
    for meeting_id, items in by_meeting.items():
        if shutdown is not None and shutdown.is_set():
            db.commit()
            logger.info("Abgebrochen nach %d/%d TOPs.", i, len(pending))
            return
        # Establish Wicket session for this meeting's to020 pages
        if meeting_id is not None:
            try:
                logger.info("to020-prep: to010 für Sitzung %d", meeting_id)
                await scraper.prepare_for_to020(meeting_id)
            except Exception as exc:
                logger.warning("prepare_for_to020 failed for meeting %s: %s", meeting_id, exc)

        # Meeting context for request log labels — computed once per group
        _mtg = db.get(Meeting, meeting_id) if meeting_id else None
        _mtg_date: str | None = (
            str(_mtg.start)[:10] if (_mtg and _mtg.start)
            else (f"~{str(_mtg.app_api_ts)[:10]}" if (_mtg and _mtg.app_api_ts) else None)
        )
        _org_lbl: str | None = None
        if _mtg and _mtg.organization_id:
            _org_tmp = db.get(Organization, _mtg.organization_id)
            _org_lbl = _org_tmp.name if _org_tmp else None
        _mtg_ctx = " ".join(p for p in [_mtg_date, _org_lbl] if p) or (
            f"Sitzung#{meeting_id}" if meeting_id else "?"
        )
        for ai in items:
            i += 1
            if shutdown is not None and shutdown.is_set():
                db.commit()
                logger.info("Abgebrochen nach %d/%d TOPs.", i - 1, len(pending))
                return
            # nichtöffentlich items are always auth-gated — skip the request
            if (ai.number or "").startswith("N") or "nichtöffentlich" in (ai.name or "").lower():
                ai.result_scraped_at = datetime.utcnow()
                if i % 50 == 0 or i == len(pending):
                    db.commit()
                    logger.info("  %s", _progress(already_done_items + i, total_items, start))
                continue
            _ai_num = (ai.number or "").rstrip(".")
            _ai_name_s = (ai.name or "")[:45].rstrip()
            _ai_label = f"{_ai_num}. {_ai_name_s}".lstrip(". ") or f"TOP#{ai.id}"
            _res_hint = f" [{ai.result}]" if ai.result else ""
            try:
                logger.debug("▶ to020 %s — %s%s", _mtg_ctx, _ai_label, _res_hint)
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
                consecutive_errors = 0
                _res_new = f" → {ai.result}" if ai.result else ""
                _anl_new = f", {len(item_files)} Anl." if item_files else ""
                logger.debug("✓ to020 %s%s%s", _ai_label, _res_new, _anl_new)
            except Exception as exc:
                consecutive_errors += 1
                logger.debug("✗ to020 %s — %s", _ai_label, str(exc)[:60])
                logger.warning(
                    "to020 TOLFDNR=%d: %s (%d konsekutiv)",
                    ai.id, exc, consecutive_errors,
                )
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    db.commit()
                    logger.error(
                        "Aborting sync_agenda_item_details after %d consecutive errors at item %d.",
                        consecutive_errors, ai.id,
                    )
                    return
            if on_progress:
                on_progress(already_done_items + i, total_items)
            if i % 50 == 0 or i == len(pending):
                db.commit()
                logger.info("  %s", _progress(already_done_items + i, total_items, start))
    logger.info("Agenda item details synced.")




async def sync_papers(
    scraper: AllrisScraper,
    db: Session,
    shutdown: asyncio.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
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
    total_papers = db.query(Paper).count()
    already_done_papers = total_papers  # pending are NOT yet in Paper table
    if not pending_ids:
        logger.info("Alle Vorlagen aktuell (%d gesamt).", total_papers)
        return
    total_after = total_papers + len(pending_ids)
    logger.info(
        "vo020 — %d ausstehend von %d gesamt (%d bereits erledigt) ...",
        len(pending_ids),
        total_after,
        already_done_papers,
    )
    start = time.monotonic()
    consecutive_errors = 0
    for i, paper_id in enumerate(pending_ids, 1):
        if shutdown is not None and shutdown.is_set():
            db.commit()
            logger.info("Abgebrochen nach %d/%d Vorlagen.", i - 1, len(pending_ids))
            return
        try:
            logger.debug("▶ vo020 Vorlage %d", paper_id)
            scraped = await scraper.scrape_paper(paper_id)
            if scraped is None:
                consecutive_errors = 0
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
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logger.warning(
                "Failed to scrape paper %d: %s (%d consecutive)",
                paper_id, exc, consecutive_errors,
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                db.commit()
                logger.error(
                    "Aborting sync_papers after %d consecutive errors at paper %d.",
                    consecutive_errors, paper_id,
                )
                return
        if on_progress:
            on_progress(already_done_papers + i, total_after)
        if i % 25 == 0 or i == len(pending_ids):
            db.commit()
            logger.info("  %s", _progress(already_done_papers + i, total_after, start))
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


async def sync_orgs_from_api(app_api: AllrisAppApi, db: Session) -> list[Organization]:
    """Create Organization stubs from action=1 gremien (no Playwright needed).

    Only sets name and short_name. Full metadata (website, organizationType)
    is filled in by sync_organizations (gr010) during the Playwright phase.
    """
    info = await app_api.get_system_info()
    created = 0
    orgs: list[Organization] = []
    for g in info.get("gremien", []):
        grlfdnr = g["grlfdnr"]
        org = db.get(Organization, grlfdnr)
        if org is None:
            org = Organization(
                id=grlfdnr,
                name=g["name"] or "",
                short_name=g["short"],
                scraped_at=datetime.utcnow(),
            )
            db.add(org)
            created += 1
        elif not org.name and g["name"]:
            org.name = g["name"]
        orgs.append(org)
    db.commit()
    logger.info("API orgs: %d from action=1 (%d new)", len(orgs), created)
    return orgs


async def sync_meetings_from_feed(app_api: AllrisAppApi, db: Session) -> tuple[int, int]:
    """Create Meeting stubs from the action=2 change feed (no Playwright needed).

    Covers the server-configured lookback window (maxZurueck years, typically 4).
    Returns (total_in_feed, new_stubs_added).
    """
    since = datetime(2000, 1, 1)  # server will cap at maxZurueck anyway
    changed = await app_api.get_changed_meetings(since)
    new_count = 0
    for entry in changed:
        silfdnr = entry["silfdnr"]
        ts_str = entry.get("timestamp", "")
        try:
            api_ts = datetime.strptime(ts_str, "%d.%m.%Y %H:%M:%S") if ts_str else None
        except ValueError:
            api_ts = None
        existing = db.get(Meeting, silfdnr)
        if existing is None:
            db.add(Meeting(id=silfdnr, name="", scraped_at=datetime.utcnow(),
                           detail_scraped_at=None, app_api_ts=api_ts))
            new_count += 1
        elif api_ts and (existing.app_api_ts is None or api_ts > existing.app_api_ts):
            existing.app_api_ts = api_ts
    db.commit()
    logger.info("action=2: %d Sitzungen im Feed, %d neu", len(changed), new_count)
    return len(changed), new_count


async def run_sync() -> None:
    """Täglicher Sync: action=1/2/4 + Playwright to010-Fallback + to020 + Vorlagen."""
    from oparl_bridge.scraper.app_api import AllrisAppApi

    init_db()
    scraper = AllrisScraper()
    async with scraper.session():
        async with AllrisAppApi() as app_api:
            with SessionLocal() as db:
                await sync_orgs_from_api(app_api, db)
                await sync_meetings_from_feed(app_api, db)
                await sync_meeting_details(scraper, db, app_api=app_api)
                await sync_agenda_item_details(scraper, db)
                await sync_papers(scraper, db)
    logger.info("sync abgeschlossen.")


async def run_initial_sync() -> None:
    """Vollständiger Erst-Sync: API-Pfad + to020/Vorlagen + gr020 + si018 + historische Details."""
    from oparl_bridge.scraper.app_api import AllrisAppApi

    init_db()
    scraper = AllrisScraper()
    async with scraper.session():
        async with AllrisAppApi() as app_api:
            with SessionLocal() as db:
                # Schneller Pfad: action=1/2/4 + to020 + Vorlagen
                logger.info("Phase 1/3: App API + Details ...")
                await sync_orgs_from_api(app_api, db)
                await sync_meetings_from_feed(app_api, db)
                await sync_meeting_details(scraper, db, app_api=app_api)
                await sync_agenda_item_details(scraper, db)
                await sync_papers(scraper, db)
                # Playwright historisch: gr020 + si018
                logger.info("Phase 2/3: Playwright — gr020 Mitglieder + si018 historisch ...")
                orgs = await sync_organizations(scraper, db)
                await sync_memberships(scraper, db, orgs)
                for org in orgs:
                    try:
                        await sync_meetings(scraper, db, organization_id=org.id)
                    except Exception as exc:
                        logger.warning("org %d: %s", org.id, exc)
                # Historische Details: nochmal to010/to020/Vorlagen für neu entdeckte Sitzungen
                logger.info("Phase 3/3: Details für historische Sitzungen ...")
                await sync_meeting_details(scraper, db, app_api=app_api)
                await sync_agenda_item_details(scraper, db)
                await sync_papers(scraper, db)
    logger.info("initial-sync abgeschlossen.")
