"""Maps DB models to OParl 1.1 Pydantic objects."""

from datetime import UTC, datetime

from oparl_bridge.config import Settings
from oparl_bridge.config import settings as default_settings
from oparl_bridge.db.models import AgendaItem, File, Meeting, Organization, Paper
from oparl_bridge.normalizer.oparl_schema import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSystem,
)


class OParlMapper:
    def __init__(self, cfg: Settings = default_settings, base_url: str | None = None) -> None:
        self.cfg = cfg
        self._base_url = (base_url or cfg.api_base_url).rstrip("/")

    def _api(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _allris(self, path: str) -> str:
        return f"{self.cfg.allris_base_url.rstrip('/')}/{path.lstrip('/')}"

    def system(self) -> OParlSystem:
        return OParlSystem(
            id=self._api("/oparl/v1.1"),
            type="https://schema.oparl.org/1.1/System",
            oparlVersion="https://schema.oparl.org/1.1/",
            name=f"oparl-bridge for {self.cfg.body_name}",
            website=self.cfg.body_website,
            vendor="oparl-bridge",
            product="https://github.com/aeroid/oparl-bridge",
            body=self._api("/oparl/v1.1/bodies"),
        )

    def body(self) -> OParlBody:
        return OParlBody(
            id=self._api("/oparl/v1.1/body/1"),
            type="https://schema.oparl.org/1.1/Body",
            system=self._api("/oparl/v1.1"),
            name=self.cfg.body_name,
            website=self.cfg.body_website,
            organization=self._api("/oparl/v1.1/body/1/organizations"),
            meeting=self._api("/oparl/v1.1/body/1/meetings"),
            paper=self._api("/oparl/v1.1/body/1/papers"),
        )

    def organization(self, org: Organization) -> OParlOrganization:
        return OParlOrganization(
            id=self._api(f"/oparl/v1.1/organization/{org.id}"),
            type="https://schema.oparl.org/1.1/Organization",
            body=self._api("/oparl/v1.1/body/1"),
            name=org.name,
            shortName=org.short_name,
            organizationType=org.organization_type,
            meeting=self._api(f"/oparl/v1.1/body/1/meetings?organization={org.id}"),
            created=org.scraped_at,
            modified=org.scraped_at,
        )

    def meeting(self, mtg: Meeting) -> OParlMeeting:
        start = _ensure_aware(mtg.start)
        organizations = (
            [self._api(f"/oparl/v1.1/organization/{mtg.organization_id}")]
            if mtg.organization_id
            else []
        )
        location = {"description": mtg.location} if mtg.location else None
        agenda_items = [self._api(f"/oparl/v1.1/agendaitem/{ai.id}") for ai in mtg.agenda_items]
        return OParlMeeting(
            id=self._api(f"/oparl/v1.1/meeting/{mtg.id}"),
            type="https://schema.oparl.org/1.1/Meeting",
            body=self._api("/oparl/v1.1/body/1"),
            name=mtg.name,
            start=start,
            location=location,
            organization=organizations,
            agendaItem=agenda_items,
            created=mtg.scraped_at,
            modified=mtg.scraped_at,
        )

    def agenda_item(self, item: AgendaItem) -> OParlAgendaItem:
        consultation = None
        if item.paper_id:
            consultation = {
                "paper": self._api(f"/oparl/v1.1/paper/{item.paper_id}"),
                "agendaItem": self._api(f"/oparl/v1.1/agendaitem/{item.id}"),
            }
        return OParlAgendaItem(
            id=self._api(f"/oparl/v1.1/agendaitem/{item.id}"),
            type="https://schema.oparl.org/1.1/AgendaItem",
            meeting=self._api(f"/oparl/v1.1/meeting/{item.meeting_id}"),
            number=item.number,
            name=item.name,
            public=item.public,
            consultation=consultation,
            created=item.scraped_at,
            modified=item.scraped_at,
        )

    def paper(self, p: Paper) -> OParlPaper:
        file_urls = [self._api(f"/oparl/v1.1/file/{f.id}") for f in p.files]
        return OParlPaper(
            id=self._api(f"/oparl/v1.1/paper/{p.id}"),
            type="https://schema.oparl.org/1.1/Paper",
            body=self._api("/oparl/v1.1/body/1"),
            name=p.name,
            reference=p.reference,
            paperType=p.paper_type,
            mainFile=file_urls[0] if file_urls else None,
            auxiliaryFile=file_urls[1:],
            created=p.scraped_at,
            modified=p.scraped_at,
        )

    def file(self, f: File) -> OParlFile:
        return OParlFile(
            id=self._api(f"/oparl/v1.1/file/{f.id}"),
            type="https://schema.oparl.org/1.1/File",
            name=f.name,
            mimeType=f.mime_type,
            accessUrl=f.access_url,
            created=f.scraped_at,
            modified=f.scraped_at,
        )


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
