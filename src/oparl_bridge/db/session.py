from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from oparl_bridge.config import settings
from oparl_bridge.db.models import Base

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
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

        ai_cols2 = {c["name"] for c in inspector.get_columns("agenda_items")}
        if "result_scraped_at" not in ai_cols2:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN result_scraped_at DATETIME"))
            conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
