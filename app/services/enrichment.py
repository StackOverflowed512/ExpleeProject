"""Explorer enrichment: combine discovery data with stored website research.

Enrichment is a read-only projection. It never fetches, never writes, and
never touches qualification or saved-prospect state (Phase 3). The same
stored rows always produce the same :class:`EnrichedCompany`, so repeated
calls are idempotent.

Page text is exposed as sourced content with its provenance (requested and
final URL, fetch time, fetcher). It is not parsed into company facts; any
field that is not stored stays ``None``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.company_research import CompanyResearch
from app.schemas.research import DiscoveryProvenance, EnrichedCompany
from app.services.research import (
    get_candidate,
    latest_usable_research,
    list_attempts,
    research_status,
    to_record,
    to_summary,
)


def build_enrichment(
    candidate: CompanyCandidate,
    latest_usable: CompanyResearch | None,
    latest_attempt: CompanyResearch | None,
) -> EnrichedCompany:
    """Pure function: combine one candidate with its research rows."""
    return EnrichedCompany(
        candidate_id=candidate.id,
        name=candidate.name,
        website_url=candidate.website_url,
        domain=candidate.domain,
        description=candidate.description,
        discovery=DiscoveryProvenance(
            provider=candidate.provider,
            provider_result_id=candidate.provider_result_id,
            source_url=candidate.source_url,
            discovered_at=candidate.discovered_at,
        ),
        research_status=research_status(latest_usable, latest_attempt),
        website=to_record(latest_usable) if latest_usable is not None else None,
        last_attempt=to_summary(latest_attempt) if latest_attempt is not None else None,
    )


def get_enrichment(session: Session, candidate_id: int) -> EnrichedCompany:
    """Load a candidate and its stored research; raises if it does not exist."""
    candidate = get_candidate(session, candidate_id)
    attempts = list_attempts(session, candidate_id)
    latest_attempt = attempts[0] if attempts else None
    return build_enrichment(
        candidate,
        latest_usable_research(session, candidate_id),
        latest_attempt,
    )
