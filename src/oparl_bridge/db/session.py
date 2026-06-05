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
        if "paper_id" not in ai_cols:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN paper_id INTEGER REFERENCES papers(id)"))
            conn.commit()
        if "paper_reference" not in ai_cols:
            conn.execute(text("ALTER TABLE agenda_items ADD COLUMN paper_reference VARCHAR(100)"))
            conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
