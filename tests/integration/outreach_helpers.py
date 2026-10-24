"""Helpers that create real saved prospects for outreach tests.

Prospects are built through the Phase 3 services (website research, then
qualification, then save), so campaign tests use genuinely eligible saved
prospects rather than rows inserted around the eligibility rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.company_research import CompanyResearch
from app.models.saved_prospect import SavedProspect
from app.schemas.qualification import QualificationCriteria
from app.services.prospects import save_prospect
from app.services.qualification import run_qualification

FETCHED_AT = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
SAAS = QualificationCriteria(target_keywords=["saas"])


def make_candidate(
    session: Session,
    *,
    name: str = "Acme Logistics",
    domain: str = "acme.com",
    description: str | None = "Acme builds SaaS for freight teams.",
    research_text: str | None = "Acme is a B2B SaaS platform for freight dispatchers.",
) -> CompanyCandidate:
    candidate = CompanyCandidate(
        name=name,
        website_url=f"https://{domain}/",
        domain=domain,
        description=description,
        provider="fake",
        source_url=f"https://{domain}/",
    )
    session.add(candidate)
    session.flush()
    if research_text is not None:
        session.add(
            CompanyResearch(
                candidate_id=candidate.id,
                requested_url=candidate.website_url,
                final_url=candidate.website_url,
                fetched_at=FETCHED_AT,
                fetcher="fake",
                outcome="success",
                page_title=f"{name} | Freight software",
                extracted_text=research_text,
            )
        )
    session.commit()
    return candidate


def make_saved_prospect(session: Session, **candidate_fields: object) -> SavedProspect:
    """Create a candidate with website research, qualify it, and save it."""
    candidate = make_candidate(session, **candidate_fields)  # type: ignore[arg-type]
    qualification = run_qualification(session, candidate.id, SAAS)
    assert qualification.status == "qualified", qualification.rationale
    return save_prospect(session, candidate.id).prospect
