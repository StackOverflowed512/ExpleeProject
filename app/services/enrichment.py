"""Enrichment: combine discovery data with already-stored research.

Enrichment is a read-only projection. It never fetches a website, so repeating
it for the same candidate and research history is idempotent and causes no
additional network traffic. The original discovery fields and provenance are
returned unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.research_record import ResearchRecord
from app.schemas.discovery import CandidateSummary
from app.schemas.enrichment import EnrichmentResponse, EnrichmentStatus
from app.services.providers.website import FetchOutcome
from app.services.research import (
    list_research,
    require_candidate,
    to_research_summary,
)

_SUCCESSFUL_OUTCOMES = (FetchOutcome.SUCCESS.value, FetchOutcome.PARTIAL.value)


def build_enrichment(
    candidate: CompanyCandidate,
    records: Sequence[ResearchRecord],
) -> EnrichmentResponse:
    """Combine a candidate with records ordered newest first.

    The latest successful record is retained even when the newest attempt
    failed. In that case ``status`` is ``stale`` so the failure is visible
    without hiding verified data.
    """
    latest_attempt = records[0] if records else None
    latest_successful = next(
        (record for record in records if record.outcome in _SUCCESSFUL_OUTCOMES),
        None,
    )

    status = _status(latest_attempt, latest_successful)
    return EnrichmentResponse(
        candidate=CandidateSummary(
            id=candidate.id,
            name=candidate.name,
            website_url=candidate.website_url,
            domain=candidate.domain,
            description=candidate.description,
            provider=candidate.provider,
            provider_result_id=candidate.provider_result_id,
            source_url=candidate.source_url,
            discovered_at=candidate.discovered_at,
            updated_at=candidate.updated_at,
        ),
        status=status,
        research=(
            to_research_summary(latest_successful)
            if latest_successful is not None
            else None
        ),
        latest_attempt=(
            to_research_summary(latest_attempt)
            if latest_attempt is not None
            else None
        ),
    )


def enrich_candidate(
    session: Session, candidate_id: int
) -> EnrichmentResponse:
    """Return the enriched view for a stored candidate.

    Raises :class:`app.services.research.CandidateNotFoundError` when the
    candidate does not exist.
    """
    candidate = require_candidate(session, candidate_id)
    return build_enrichment(candidate, list_research(session, candidate.id))


def _status(
    latest_attempt: ResearchRecord | None,
    latest_successful: ResearchRecord | None,
) -> EnrichmentStatus:
    if latest_attempt is None:
        return "not_researched"
    if latest_successful is None:
        return "failed"
    if latest_attempt.outcome == FetchOutcome.FAILED.value:
        return "stale"
    if latest_attempt.outcome == FetchOutcome.PARTIAL.value:
        return "partial"
    return "researched"
