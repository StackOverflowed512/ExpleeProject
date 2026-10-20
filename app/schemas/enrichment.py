"""Response schema for discovery-plus-research enrichment."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.discovery import CandidateSummary
from app.schemas.research import ResearchRecordSummary

EnrichmentStatus = Literal[
    "not_researched",
    "researched",
    "partial",
    "stale",
    "failed",
]


class EnrichmentResponse(BaseModel):
    """A discovered candidate combined with its stored research.

    The candidate fields are the original discovery data and provenance. The
    ``research`` field is the latest successful or partial attempt; a later
    failed refresh appears in ``latest_attempt`` and sets status to ``stale``
    without erasing what was verified earlier.
    """

    candidate: CandidateSummary
    status: EnrichmentStatus = Field(
        description=(
            "not_researched: no attempts. researched: latest successful "
            "attempt has full text. partial: usable but incomplete content. "
            "stale: a later attempt failed but earlier research remains. "
            "failed: no successful research exists."
        )
    )
    research: ResearchRecordSummary | None = Field(
        default=None,
        description="Most recent successful or partial research, if any.",
    )
    latest_attempt: ResearchRecordSummary | None = Field(
        default=None,
        description="Most recent research attempt, including failures.",
    )
