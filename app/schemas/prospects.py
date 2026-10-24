"""Saved prospect request, query, and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.discovery import CandidateSummary
from app.schemas.qualification import QualificationRecord, QualificationSummary

ProspectSort = Literal["saved_at_desc", "saved_at_asc", "score_desc", "name_asc"]
MAX_PAGE_SIZE = 100
MAX_OFFSET = 10_000


class SaveProspectRequest(BaseModel):
    """Body of ``POST /companies/{id}/save``.

    Omit ``qualification_id`` to use the candidate's latest qualification.
    """

    model_config = ConfigDict(extra="forbid")

    qualification_id: int | None = Field(
        default=None,
        ge=1,
        le=2_147_483_647,
        description="A qualification of this candidate with status 'qualified'.",
    )


class ProspectSummary(BaseModel):
    """A saved prospect with the company basics and justifying qualification."""

    id: int
    candidate_id: int
    name: str
    domain: str | None = None
    website_url: str | None = None
    saved_at: datetime
    updated_at: datetime
    qualification: QualificationSummary


class SaveProspectResponse(BaseModel):
    """Result of a save request.

    ``created`` is false when the candidate was already saved; the existing
    prospect is returned unchanged.
    """

    created: bool
    prospect: ProspectSummary


class ProspectDetail(BaseModel):
    """A saved prospect with its candidate and full qualification record."""

    id: int
    saved_at: datetime
    updated_at: datetime
    candidate: CandidateSummary
    qualification: QualificationRecord


class ProspectQuery(BaseModel):
    """Validated filters, search, ordering, and pagination for ``GET /prospects``."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        description="Case-insensitive substring match on name, domain, or description.",
    )
    domain: str | None = Field(
        default=None,
        min_length=3,
        max_length=255,
        description="Exact normalized domain (for example 'acme.com').",
    )
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)
    saved_after: datetime | None = Field(
        default=None, description="Inclusive lower bound on saved_at (ISO 8601)."
    )
    saved_before: datetime | None = Field(
        default=None, description="Exclusive upper bound on saved_at (ISO 8601)."
    )
    sort: ProspectSort = Field(
        default="saved_at_desc",
        description="Ordering; ties are always broken by prospect ID.",
    )
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)

    @field_validator("q", "domain", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("domain")
    @classmethod
    def _lower_domain(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def _check_ranges(self) -> "ProspectQuery":
        if (
            self.min_score is not None
            and self.max_score is not None
            and self.min_score > self.max_score
        ):
            raise ValueError("min_score must not exceed max_score.")
        if (
            self.saved_after is not None
            and self.saved_before is not None
            and self.saved_after >= self.saved_before
        ):
            raise ValueError("saved_after must be earlier than saved_before.")
        for name in ("saved_after", "saved_before"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include a timezone offset.")
        return self


class ProspectListResponse(BaseModel):
    """One page of saved prospects."""

    items: list[ProspectSummary]
    total: int = Field(description="Prospects matching the filters.")
    limit: int
    offset: int
    sort: ProspectSort
