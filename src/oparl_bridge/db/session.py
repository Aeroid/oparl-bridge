from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from oparl_bridge.config import settings
from oparl_bridge.db.models import Base

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
        cur.execute("PRAGMA mmap_size=134217728") # 128 MB memory-mapped I/O
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate(engine)


def _migrate(eng) -> None:
    """Apply lightweight schema migrations for columns added after initial creation."""
    inspector = inspect(eng)
    with eng.connect() as conn:
        meetings_cols = {c["name"] for c in inspector.get_columns("meetings")}
        if "detail_scraped_at" not in meetings_cols:
            conn.execute(text("ALTER TABLE meetings ADD COLUMN detail_scraped_at DATETIME"))
            conn.commit()

        ai_cols = {c["name"] for c in inspector.get_columns("agenda_items")}
        for col, defn in [
            ("paper_id", "INTEGER REFERENCES papers(id)"),
            ("paper_reference", "VARCHAR(100)"),
            ("result", "VARCHAR(50)"),
            ("resolution_text", "TEXT"),
            ("vote_text", "TEXT"),
        ]:
            if col not in ai_cols:
                conn.execute(text(f"ALTER TABLE agenda_items ADD COLUMN {col} {defn}"))
                conn.commit()

        file_cols = {c["name"] for c in inspector.get_columns("files")}
        if "agenda_item_id" not in file_cols:
            conn.execute(text(
                "ALTER TABLE files ADD COLUMN agenda_item_id INTEGER REFERENCES agenda_items(id)"
            ))
            conn.commit()
        file_cols2 = {c["name"] for c in inspector.get_columns("files")}
        if "meeting_id" not in file_cols2:
            conn.execute(text(
                "ALTER TABLE files ADD COLUMN meeting_id INTEGER REFERENCES meetings(id)"
            ))
            conn.commit()

        ai_cols2 = {c["name"] for c in inspector.get_columns("agenda_items")}
        if "result_scraped_at" not in ai_cols2:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN result_scraped_at DATETIME"))
            conn.commit()

        ai_cols3 = {c["name"] for c in inspector.get_columns("agenda_items")}
        if "word_contribution" not in ai_cols3:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN word_contribution TEXT"))
            conn.commit()

        org_cols = {c["name"] for c in inspector.get_columns("organizations")}
        if "next_meeting_date" not in org_cols:
            conn.execute(text("ALTER TABLE organizations ADD COLUMN next_meeting_date VARCHAR(20)"))
            conn.commit()
        org_cols2 = {c["name"] for c in inspector.get_columns("organizations")}
        if "future_meeting_dates" not in org_cols2:
            conn.execute(text("ALTER TABLE organizations ADD COLUMN future_meeting_dates TEXT"))
            conn.commit()

        existing_indexes = {i["name"] for i in inspector.get_indexes("meetings")}
        for idx, ddl in [
            ("ix_meetings_org_id",          "CREATE INDEX IF NOT EXISTS ix_meetings_org_id ON meetings(organization_id)"),
            ("ix_meetings_detail_scraped",  "CREATE INDEX IF NOT EXISTS ix_meetings_detail_scraped ON meetings(detail_scraped_at)"),
        ]:
            if idx not in existing_indexes:
                conn.execute(text(ddl))
                conn.commit()

        ai_indexes = {i["name"] for i in inspector.get_indexes("agenda_items")}
        for idx, ddl in [
            ("ix_ai_meeting_id",        "CREATE INDEX IF NOT EXISTS ix_ai_meeting_id ON agenda_items(meeting_id)"),
            ("ix_ai_result_scraped",    "CREATE INDEX IF NOT EXISTS ix_ai_result_scraped ON agenda_items(result_scraped_at)"),
            ("ix_ai_paper_id",          "CREATE INDEX IF NOT EXISTS ix_ai_paper_id ON agenda_items(paper_id)"),
        ]:
            if idx not in ai_indexes:
                conn.execute(text(ddl))
                conn.commit()

        file_indexes = {i["name"] for i in inspector.get_indexes("files")}
        for idx, ddl in [
            ("ix_files_meeting_id",      "CREATE INDEX IF NOT EXISTS ix_files_meeting_id ON files(meeting_id)"),
            ("ix_files_agenda_item_id",  "CREATE INDEX IF NOT EXISTS ix_files_agenda_item_id ON files(agenda_item_id)"),
        ]:
            if idx not in file_indexes:
                conn.execute(text(ddl))
                conn.commit()

        # App API columns — added for ALLRIS /app01 integration
        file_cols3 = {c["name"] for c in inspector.get_columns("files")}
        for col, defn in [
            ("doc_guid", "VARCHAR(100)"),
            ("doc_crc",  "VARCHAR(20)"),
        ]:
            if col not in file_cols3:
                conn.execute(text(f"ALTER TABLE files ADD COLUMN {col} {defn}"))
                conn.commit()

        ai_cols4 = {c["name"] for c in inspector.get_columns("agenda_items")}
        if "beratung_id" not in ai_cols4:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN beratung_id INTEGER"))
            conn.commit()

        # action=4 Beschluss metadata
        ai_cols5 = {c["name"] for c in inspector.get_columns("agenda_items")}
        for col, defn in [
            ("beschluss_datum", "VARCHAR(20)"),
            ("beschluss_totyp", "INTEGER"),
            ("protokoll_guid",  "VARCHAR(100)"),
            ("protokoll_crc",   "VARCHAR(20)"),
        ]:
            if col not in ai_cols5:
                conn.execute(text(f"ALTER TABLE agenda_items ADD COLUMN {col} {defn}"))
                conn.commit()

        mtg_cols = {c["name"] for c in inspector.get_columns("meetings")}
        for col, defn in [
            ("bekanntmachung_guid", "VARCHAR(100)"),
            ("protokoll_guid",      "VARCHAR(100)"),
            ("protokoll_crc",       "VARCHAR(20)"),
            ("app_api_ts",          "DATETIME"),
        ]:
            if col not in mtg_cols:
                conn.execute(text(f"ALTER TABLE meetings ADD COLUMN {col} {defn}"))
                conn.commit()

        # meeting_documents table (created via Base.metadata if new, migrated if old)
        if not inspect(eng).has_table("meeting_documents"):
            conn.execute(text("""
                CREATE TABLE meeting_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
                    guid TEXT NOT NULL,
                    typ INTEGER NOT NULL,
                    dotimestamp TEXT,
                    crc TEXT,
                    UNIQUE(meeting_id, guid)
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_meetdoc_meeting_id ON meeting_documents(meeting_id)"
            ))
            conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
