"""Website research and enrichment request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResearchOutcome = Literal["success", "partial", "empty", "failed"]
ResearchStatus = Literal["available", "partial", "unavailable", "not_researched"]

_STATUS_DESCRIPTION = (
    "'available': complete usable research exists. 'partial': the latest "
    "usable research hit a size limit. 'unavailable': research was attempted "
    "but no attempt produced readable text. 'not_researched': no attempt yet."
)


class ResearchRequest(BaseModel):
    """Options for requesting website research.

    The fetch target is always the stored candidate website; callers cannot
    supply a URL, and unknown fields are rejected.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"refresh": False}]},
    )

    refresh: bool = Field(
        default=False,
        description=(
            "Fetch again even when fresh usable research exists. Without it, "
            "research younger than RESEARCH_FRESHNESS_HOURS is reused."
        ),
    )


class ResearchAttemptSummary(BaseModel):
    """Metadata of one research attempt, without the extracted text."""

    id: int
    candidate_id: int
    outcome: ResearchOutcome
    failure_category: str | None = Field(
        default=None,
        description="Safe failure category when outcome is 'failed'.",
    )
    requested_url: str = Field(description="Stored candidate website that was requested.")
    final_url: str | None = Field(
        default=None, description="Last URL requested after permitted redirects."
    )
    http_status: int | None = None
    content_type: str | None = None
    page_title: str | None = None
    content_truncated: bool = Field(
        description="Whether the response body or extracted text hit a size limit."
    )
    fetcher: str = Field(description="Adapter that performed the fetch.")
    fetched_at: datetime
    created_at: datetime
    updated_at: datetime


class ResearchRecord(ResearchAttemptSummary):
    """A research attempt including its extracted page text.

    ``extracted_text`` is visible page text from ``final_url``. It is
    third-party content, not verified company facts.
    """

    extracted_text: str | None = None


class ResearchRunResponse(BaseModel):
    """Result of requesting research for one candidate."""

    candidate_id: int
    status: ResearchStatus = Field(description=_STATUS_DESCRIPTION)
    reused: bool = Field(
        description=(
            "True when fresh usable research was returned without fetching."
        )
    )
    attempt: ResearchAttemptSummary = Field(
        description="The attempt made by this request, or the reused record."
    )
    latest_usable: ResearchRecord | None = Field(
        default=None,
        description=(
            "Most recent successful or partial research. A failed attempt "
            "never replaces it."
        ),
    )


class ResearchHistoryResponse(BaseModel):
    """Stored research for one candidate."""

    candidate_id: int
    status: ResearchStatus = Field(description=_STATUS_DESCRIPTION)
    latest_usable: ResearchRecord | None = None
    attempts: list[ResearchAttemptSummary] = Field(
        description="Retained attempts, newest first."
    )


class DiscoveryProvenance(BaseModel):
    """Where and when the candidate was discovered (unchanged by research)."""

    provider: str
    provider_result_id: str | None = None
    source_url: str
    discovered_at: datetime


class EnrichedCompany(BaseModel):
    """Discovery data combined with the latest usable website research.

    Fields are either sourced or ``None``. ``description`` is the discovery
    provider's snippet; ``website`` holds page content with its own
    provenance. No facts are inferred from page text.
    """

    candidate_id: int
    name: str
    website_url: str | None = None
    domain: str | None = None
    description: str | None = None
    discovery: DiscoveryProvenance
    research_status: ResearchStatus = Field(description=_STATUS_DESCRIPTION)
    website: ResearchRecord | None = Field(
        default=None,
        description="Latest usable research, or null when none exists.",
    )
    last_attempt: ResearchAttemptSummary | None = Field(
        default=None,
        description="Most recent attempt of any outcome, or null.",
    )
