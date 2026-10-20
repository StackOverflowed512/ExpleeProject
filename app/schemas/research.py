"""Request and response schemas for website research."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.providers.website import FetchFailureCategory, FetchOutcome


class ResearchRequest(BaseModel):
    """Optional research request body.

    Research always uses the website URL already stored on the company
    candidate. The body intentionally accepts no fields, so callers cannot
    submit an arbitrary URL to fetch.
    """

    model_config = ConfigDict(extra="forbid")


class ResearchRecordSummary(BaseModel):
    """One stored research attempt with its provenance."""

    id: int
    company_candidate_id: int
    requested_url: str = Field(
        description="Website URL requested from the candidate's stored data."
    )
    final_url: str | None = Field(
        default=None,
        description="Final URL after permitted redirects, when one was reached.",
    )
    fetched_at: datetime = Field(
        description="When the website fetch completed (UTC)."
    )
    http_status: int | None = Field(
        default=None, description="HTTP status code, when a response was read."
    )
    content_type: str | None = Field(
        default=None, description="Response content type, when available."
    )
    page_title: str | None = Field(
        default=None,
        description="Visible page title, or null when the page had none.",
    )
    page_text: str | None = Field(
        default=None,
        description=(
            "Extracted visible text, capped by configuration. Null means no "
            "content was extracted; an empty string means the page had no "
            "visible text."
        ),
    )
    outcome: FetchOutcome = Field(
        description="success, partial, or failed fetch outcome."
    )
    failure_category: FetchFailureCategory | None = Field(
        default=None,
        description="Safe failure category when outcome is failed.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether extracted text was capped by the text limit.",
    )
    created_at: datetime = Field(description="When the record was stored.")
    updated_at: datetime = Field(description="When the record was last updated.")


class ResearchResponse(ResearchRecordSummary):
    """Result of one research request."""

    created: bool = Field(
        description=(
            "True when a new record was stored. False when an identical "
            "consecutive attempt returned the existing record."
        )
    )


class ResearchHistoryResponse(BaseModel):
    """All stored research attempts for a candidate, newest first."""

    candidate_id: int
    records: list[ResearchRecordSummary] = Field(
        description="Append-only research history, newest first."
    )
    latest_successful: ResearchRecordSummary | None = Field(
        default=None,
        description=(
            "Most recent successful or partial record. A later failure does "
            "not remove it."
        ),
    )
