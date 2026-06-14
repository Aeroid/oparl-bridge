from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # GRLFDNR
    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str | None] = mapped_column(String(100))
    organization_type: Mapped[str | None] = mapped_column(String(100))
    next_meeting_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    future_meeting_dates: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of ISO datetimes
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    meetings: Mapped[list["Meeting"]] = relationship(back_populates="organization")
    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # SILFDNR
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(500))
    start: Mapped[datetime | None] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(String(500))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    detail_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    app_api_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bekanntmachung_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    protokoll_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    protokoll_crc: Mapped[str | None] = mapped_column(String(20), nullable=True)

    organization: Mapped["Organization | None"] = relationship(back_populates="meetings")
    agenda_items: Mapped[list["AgendaItem"]] = relationship(back_populates="meeting")
    files: Mapped[list["File"]] = relationship(back_populates="meeting")
    meeting_documents: Mapped[list["MeetingDocument"]] = relationship(back_populates="meeting")


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # VOLFDNR
    name: Mapped[str] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(100))
    paper_type: Mapped[str | None] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    agenda_items: Mapped[list["AgendaItem"]] = relationship(back_populates="paper")
    files: Mapped[list["File"]] = relationship(back_populates="paper")


class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # TOLFDNR
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id"))
    paper_reference: Mapped[str | None] = mapped_column(String(100))  # e.g. "VO/26/04523"
    number: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(500))
    public: Mapped[bool] = mapped_column(default=True)
    # ACCEPTED / REJECTED / DEFERRED / NODECISION
    result: Mapped[str | None] = mapped_column(String(50))
    resolution_text: Mapped[str | None] = mapped_column(Text)  # raw Beschlusstext
    vote_text: Mapped[str | None] = mapped_column(Text)         # raw Abstimmungsergebnis
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    word_contribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    beratung_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # beratung lfdnr from App API
    beschluss_datum: Mapped[str | None] = mapped_column(String(20), nullable=True)   # ISO date of Beschluss
    beschluss_totyp: Mapped[int | None] = mapped_column(Integer, nullable=True)       # raw totyp bitmask
    protokoll_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)    # Protokollauszug (typ=138)
    protokoll_crc: Mapped[str | None] = mapped_column(String(20), nullable=True)

    meeting: Mapped["Meeting | None"] = relationship(back_populates="agenda_items")
    paper: Mapped["Paper | None"] = relationship(back_populates="agenda_items")
    files: Mapped[list["File"]] = relationship(
        back_populates="agenda_item",
        foreign_keys="[File.agenda_item_id]",
    )


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id"))
    agenda_item_id: Mapped[int | None] = mapped_column(ForeignKey("agenda_items.id"))
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    name: Mapped[str] = mapped_column(String(500))
    access_url: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    doc_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)  # ALLRIS document GUID from App API
    doc_crc: Mapped[str | None] = mapped_column(String(20), nullable=True)    # CRC32 hex for change detection

    paper: Mapped["Paper | None"] = relationship(back_populates="files")
    agenda_item: Mapped["AgendaItem | None"] = relationship(back_populates="files")
    meeting: Mapped["Meeting | None"] = relationship(back_populates="files")


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # KPLFDNR
    name: Mapped[str] = mapped_column(String(200))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="person")


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("persons.id"))
    organization_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"))
    role: Mapped[str | None] = mapped_column(String(200))

    person: Mapped["Person"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")


class MeetingDocument(Base):
    """All documents attached to a meeting (Bekanntmachung, Protokoll, Einladung, …).

    Populated from action=4 <dokumente>. Serves as a lightweight index for CRC-based
    change detection and guid→typ lookup for the PDF proxy.
    """
    __tablename__ = "meeting_documents"
    __table_args__ = (UniqueConstraint("meeting_id", "guid", name="uq_meeting_doc"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"))
    guid: Mapped[str] = mapped_column(String(100))
    typ: Mapped[int] = mapped_column(Integer)
    dotimestamp: Mapped[str | None] = mapped_column(String(30), nullable=True)
    crc: Mapped[str | None] = mapped_column(String(20), nullable=True)

    meeting: Mapped["Meeting"] = relationship(back_populates="meeting_documents")


