from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # GRLFDNR
    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str | None] = mapped_column(String(100))
    organization_type: Mapped[str | None] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    meetings: Mapped[list["Meeting"]] = relationship(back_populates="organization")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # SILFDNR
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(500))
    start: Mapped[datetime | None] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(String(500))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    detail_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    organization: Mapped["Organization | None"] = relationship(back_populates="meetings")
    agenda_items: Mapped[list["AgendaItem"]] = relationship(back_populates="meeting")


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
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    meeting: Mapped["Meeting | None"] = relationship(back_populates="agenda_items")
    paper: Mapped["Paper | None"] = relationship(back_populates="agenda_items")


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id"))
    name: Mapped[str] = mapped_column(String(500))
    access_url: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    paper: Mapped["Paper | None"] = relationship(back_populates="files")
