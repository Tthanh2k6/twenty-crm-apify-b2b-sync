"""
Strict Pydantic v2 data models matching:
1. Input records emitted by Apify Actor `thanh81646/apify-b2b-lead-and-contact-enricher`
2. Twenty CRM 2026 Standard Workspace Entities (`Company`, `Person`, `Opportunity`, `Note`, `NoteTarget`)
   including composite metadata structures (`LinksMetadata`, `EmailsMetadata`, `PhonesMetadata`,
   `FullNameMetadata`, `AddressMetadata`, `CurrencyMetadata`, `RichTextMetadata`).
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 1. Source Schema: Apify Actor (thanh81646/apify-b2b-lead-and-contact-enricher)
# ---------------------------------------------------------------------------


class ApifySocialProfiles(BaseModel):
    model_config = ConfigDict(extra="allow")

    linkedin: str | None = None
    twitter_x: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    youtube: str | None = None
    tiktok: str | None = None


class ApifyEnrichedLead(BaseModel):
    """Output item produced by `thanh81646/apify-b2b-lead-and-contact-enricher`."""

    model_config = ConfigDict(extra="ignore")

    url: str
    company_name: str = ""
    meta_description: str = ""
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    social_profiles: ApifySocialProfiles = Field(default_factory=ApifySocialProfiles)
    tech_stack: list[str] = Field(default_factory=list)
    json_ld_types: list[str] = Field(default_factory=list)
    pages_crawled: int = 1
    status_code: int | None = 200
    error: str | None = None
    city: str | None = None
    country: str | None = None
    estimated_deal_usd: float | None = None


# ---------------------------------------------------------------------------
# 2. Target Schema: Twenty CRM Composite Field Types (twenty-shared/types)
# ---------------------------------------------------------------------------


class SecondaryLinkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    label: str = ""


class LinksMetadata(BaseModel):
    """Matches Twenty `LinksMetadata` composite field (`domainName`, `linkedinLink`)."""

    model_config = ConfigDict(extra="forbid")

    primaryLinkUrl: str
    primaryLinkLabel: str = ""
    secondaryLinks: list[SecondaryLinkItem] = Field(default_factory=list)


class EmailsMetadata(BaseModel):
    """Matches Twenty `EmailsMetadata` composite field on `Person`."""

    model_config = ConfigDict(extra="forbid")

    primaryEmail: str
    additionalEmails: list[str] = Field(default_factory=list)


class AdditionalPhoneItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    callingCode: str = ""
    countryCode: str = ""


class PhonesMetadata(BaseModel):
    """Matches Twenty `PhonesMetadata` composite field on `Person`."""

    model_config = ConfigDict(extra="forbid")

    primaryPhoneNumber: str
    primaryPhoneCountryCode: str = ""
    primaryPhoneCallingCode: str = ""
    additionalPhones: list[AdditionalPhoneItem] = Field(default_factory=list)


class FullNameMetadata(BaseModel):
    """Matches Twenty `FullNameMetadata` composite field on `Person`."""

    model_config = ConfigDict(extra="forbid")

    firstName: str
    lastName: str = ""


class AddressMetadata(BaseModel):
    """Matches Twenty `AddressMetadata` composite field on `Company`."""

    model_config = ConfigDict(extra="forbid")

    addressStreet1: str = ""
    addressStreet2: str = ""
    addressCity: str = ""
    addressPostcode: str = ""
    addressState: str = ""
    addressCountry: str = ""


class CurrencyMetadata(BaseModel):
    """Matches Twenty `CurrencyMetadata` composite field (`amountMicros` + `currencyCode`)."""

    model_config = ConfigDict(extra="forbid")

    amountMicros: int
    currencyCode: str = "USD"


class RichTextV2Metadata(BaseModel):
    """Matches Twenty `RichTextMetadata` (`bodyV2`) on `Note`."""

    model_config = ConfigDict(extra="forbid")

    markdown: str


# ---------------------------------------------------------------------------
# 3. Target REST Payloads (`POST / PATCH /rest/*`)
# ---------------------------------------------------------------------------


class CompanyUpsertPayload(BaseModel):
    """Matches `CompanyWorkspaceEntity` writable standard properties."""

    model_config = ConfigDict(extra="forbid")

    name: str
    domainName: LinksMetadata
    linkedinLink: LinksMetadata | None = None
    address: AddressMetadata | None = None

    def to_twenty_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class PersonUpsertPayload(BaseModel):
    """Matches `PersonWorkspaceEntity` writable standard properties."""

    model_config = ConfigDict(extra="forbid")

    name: FullNameMetadata
    emails: EmailsMetadata
    phones: PhonesMetadata | None = None
    linkedinLink: LinksMetadata | None = None
    jobTitle: str | None = None
    companyId: str

    def to_twenty_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class OpportunityCreatePayload(BaseModel):
    """Matches `OpportunityWorkspaceEntity` writable standard properties."""

    model_config = ConfigDict(extra="forbid")

    name: str
    stage: str = "NEW"
    amount: CurrencyMetadata | None = None
    companyId: str
    pointOfContactId: str | None = None

    def to_twenty_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class NoteCreatePayload(BaseModel):
    """Matches `NoteWorkspaceEntity` writable standard properties."""

    model_config = ConfigDict(extra="forbid")

    title: str
    bodyV2: RichTextV2Metadata

    def to_twenty_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class NoteTargetCreatePayload(BaseModel):
    """Matches `NoteTargetWorkspaceEntity` linking a Note to a Company/Person."""

    model_config = ConfigDict(extra="forbid")

    noteId: str
    targetCompanyId: str | None = None
    targetPersonId: str | None = None

    def to_twenty_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class SyncRunSummary(BaseModel):
    """Structured summary emitted at the end of a sync execution."""

    mode: str
    input_records: int = 0
    unique_domains: int = 0
    skipped_errored_leads: int = 0
    skipped_ledger_duplicates: int = 0
    rejected_emails: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    people_created: int = 0
    people_updated: int = 0
    notes_attached: int = 0
    opportunities_created: int = 0
