"""Pydantic v2 models for OParl 1.1 objects."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

# OParl uses string URLs as IDs; alias them for clarity
OParlUrl = Annotated[str, Field()]


class OParlBase(BaseModel):
    model_config = {"populate_by_name": True}

    id: OParlUrl
    type: str
    created: datetime | None = None
    modified: datetime | None = None
    deleted: bool | None = None


class OParlSystem(OParlBase):
    type: str = "https://schema.oparl.org/1.1/System"
    oparl_version: str = Field("https://schema.oparl.org/1.1/", alias="oparlVersion")
    name: str | None = None
    contact_email: str | None = Field(None, alias="contactEmail")
    contact_name: str | None = Field(None, alias="contactName")
    website: str | None = None
    vendor: str | None = None
    product: str | None = None
    body: str  # URL to body list endpoint


class OParlBody(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Body"
    system: OParlUrl
    name: str
    short_name: str | None = Field(None, alias="shortName")
    website: str | None = None
    license: str | None = None
    license_valid_since: datetime | None = Field(None, alias="licenseValidSince")
    oparl_since: datetime | None = Field(None, alias="oparlSince")
    ags: str | None = None  # Amtlicher Gemeindeschlüssel
    rgs: str | None = None  # Regionalschlüssel
    equivalent: list[str] = Field(default_factory=list)
    contact_email: str | None = Field(None, alias="contactEmail")
    contact_name: str | None = Field(None, alias="contactName")
    organization: str  # URL to organization list
    meeting: str  # URL to meeting list
    paper: str  # URL to paper list
    person: str  # URL to person list
    system_name: str | None = Field(None, alias="systemName")
    legislative_term: list = Field(default_factory=list, alias="legislativeTerm")


class OParlOrganization(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Organization"
    body: OParlUrl
    name: str
    short_name: str | None = Field(None, alias="shortName")
    organization_type: str | None = Field(None, alias="organizationType")
    post: list[str] = Field(default_factory=list)
    membership: list[str] = Field(default_factory=list)
    meeting: str | None = None  # URL to filtered meeting list
    website: str | None = None
    location: dict | None = None


class OParlMeeting(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Meeting"
    body: OParlUrl
    name: str
    start: datetime | None = None
    end: datetime | None = None
    location: dict | None = None
    organization: list[OParlUrl] = Field(default_factory=list)
    agenda_item: list[str] = Field(default_factory=list, alias="agendaItem")
    invitation: str | None = None
    results_protocol: str | None = Field(None, alias="resultsProtocol")
    verbatim_protocol: str | None = Field(None, alias="verbatimProtocol")
    auxiliary_file: list[str] = Field(default_factory=list, alias="auxiliaryFile")
    public: bool = True


class OParlAgendaItem(OParlBase):
    type: str = "https://schema.oparl.org/1.1/AgendaItem"
    meeting: OParlUrl
    number: str | None = None
    name: str
    public: bool = True
    consultation: dict | None = None
    result: str | None = None
    resolution_text: str | None = Field(None, alias="resolutionText")
    resolution_file: str | None = Field(None, alias="resolutionFile")
    resolution_date: str | None = Field(None, alias="resolutionDate")
    auxiliary_file: list[str] = Field(default_factory=list, alias="auxiliaryFile")


class OParlPaper(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Paper"
    body: OParlUrl
    name: str
    reference: str | None = None
    published_date: datetime | None = Field(None, alias="publishedDate")
    paper_type: str | None = Field(None, alias="paperType")
    related_paper: list[OParlUrl] = Field(default_factory=list, alias="relatedPaper")
    superordinated_paper: list[OParlUrl] = Field(default_factory=list, alias="superordinatedPaper")
    subordinated_paper: list[OParlUrl] = Field(default_factory=list, alias="subordinatedPaper")
    main_file: str | None = Field(None, alias="mainFile")
    auxiliary_file: list[str] = Field(default_factory=list, alias="auxiliaryFile")
    location: list[dict] = Field(default_factory=list)
    originator_person: list[OParlUrl] = Field(default_factory=list, alias="originatorPerson")
    under_direction_of: list[OParlUrl] = Field(default_factory=list, alias="underDirectionOf")
    originator_organization: list[OParlUrl] = Field(
        default_factory=list, alias="originatorOrganization"
    )
    consultation: list[dict] = Field(default_factory=list)


class OParlFile(OParlBase):
    type: str = "https://schema.oparl.org/1.1/File"
    name: str | None = None
    file_name: str | None = Field(None, alias="fileName")
    mime_type: str | None = Field(None, alias="mimeType")
    date: datetime | None = None
    size: int | None = None
    sha1_checksum: str | None = Field(None, alias="sha1Checksum")
    access_url: str = Field(..., alias="accessUrl")
    download_url: str | None = Field(None, alias="downloadUrl")
    external_service_url: str | None = Field(None, alias="externalServiceUrl")
    master_file: str | None = Field(None, alias="masterFile")
    derivative_file: list[str] = Field(default_factory=list, alias="derivativeFile")
    file_license: str | None = Field(None, alias="fileLicense")
    text: str | None = None


class OParlPerson(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Person"
    body: OParlUrl
    name: str
    membership: list[OParlUrl] = Field(default_factory=list)


class OParlMembership(OParlBase):
    type: str = "https://schema.oparl.org/1.1/Membership"
    person: OParlUrl
    organization: OParlUrl
    role: str | None = None
    voting_right: bool | None = Field(None, alias="votingRight")


class OParlList(BaseModel):
    """Generic OParl paginated list wrapper."""

    data: list
    links: dict = Field(default_factory=dict)
    pagination: dict = Field(default_factory=dict)
