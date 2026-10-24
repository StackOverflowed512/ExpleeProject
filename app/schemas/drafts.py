"""Outreach draft request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.campaigns import MAX_CAMPAIGN_PROSPECTS, MAX_DB_ID

DraftState = Literal["pending"]
GenerationOutcome = Literal["generated", "existing"]


class GenerateDraftsRequest(BaseModel):
    """Body of ``POST /campaigns/{id}/drafts`` (optional)."""

    model_config = ConfigDict(extra="forbid")

    saved_prospect_ids: (
        Annotated[
            list[Annotated[int, Field(ge=1, le=MAX_DB_ID)]],
            Field(min_length=1, max_length=MAX_CAMPAIGN_PROSPECTS),
        ]
        | None
    ) = Field(
        default=None,
        description="Limit generation to these campaign prospects; omit for all.",
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Create a new revision even for prospects that already have a draft. "
            "Earlier drafts are kept, never overwritten."
        ),
    )

    @field_validator("saved_prospect_ids")
    @classmethod
    def _no_duplicates(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("saved_prospect_ids must not repeat")
        return value


class DraftRecord(BaseModel):
    """A persisted outreach draft with its audit trail."""

    id: int
    campaign_id: int
    saved_prospect_id: int
    revision: int
    state: DraftState
    subject: str
    body: str
    prompt_version: str
    prompt_sha256: str = Field(description="SHA-256 of the exact prompt messages sent.")
    provider: str
    model: str
    generation_metadata: dict[str, Any] = Field(
        description="Safe provider facts: response ID, finish reason, token usage."
    )
    provenance: dict[str, Any] = Field(
        description="Candidate, qualification, research, and evidence sources used."
    )
    qualification_id: int | None = None
    research_id: int | None = None
    generated_at: datetime
    created_at: datetime
    updated_at: datetime


class DraftGenerationItem(BaseModel):
    """What happened for one campaign prospect in a generation request."""

    saved_prospect_id: int
    outcome: GenerationOutcome = Field(
        description="'generated': a new draft was created; 'existing': skipped "
        "because a draft already exists and regenerate was false."
    )
    draft: DraftRecord


class GenerateDraftsResponse(BaseModel):
    """Result of one generation request."""

    campaign_id: int
    batch_limit: int = Field(description="Maximum drafts generated per request.")
    results: list[DraftGenerationItem]
    remaining: int = Field(
        description="Targeted prospects still waiting because of batch_limit; "
        "repeat the request to continue."
    )


class DraftListResponse(BaseModel):
    """One page of a campaign's drafts in campaign order, then revision."""

    items: list[DraftRecord]
    total: int
    limit: int
    offset: int
