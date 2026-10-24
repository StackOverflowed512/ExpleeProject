"""Explorer qualification: evaluate stored evidence and persist every run.

The service loads the candidate's enrichment (discovery fields plus latest
usable research), applies :func:`app.services.qualification_rules.evaluate`,
and appends a ``company_qualifications`` row. It never fetches, never edits
candidates or research, and never updates or deletes earlier runs, so the
qualification history is complete and auditable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Select, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.company_qualification import CompanyQualification
from app.schemas.qualification import (
    FactorOutcome,
    QualificationCriteria,
    QualificationRecord,
    QualificationStatus,
    QualificationSummary,
    StoredCriteria,
)
from app.services.enrichment import get_enrichment
from app.services.qualification_rules import RULE_VERSION, evaluate
from app.services.research import get_candidate

logger = logging.getLogger(__name__)

_POSITIVE_FACTORS = frozenset({"industry_fit", "location_fit"})
# Rule versions whose 'qualified' results may justify saving a prospect.
SAVEABLE_RULE_VERSIONS = frozenset({RULE_VERSION})


class QualificationPersistenceError(Exception):
    """A qualification run could not be saved."""


def run_qualification(
    session: Session,
    candidate_id: int,
    criteria: QualificationCriteria,
    *,
    now: datetime | None = None,
) -> CompanyQualification:
    """Qualify one candidate from stored data and persist the run.

    Raises :class:`app.services.research.CandidateNotFoundError` for an
    unknown ID and :class:`QualificationPersistenceError` (after rolling
    back) when the run cannot be saved.
    """
    enriched = get_enrichment(session, candidate_id)
    evaluation = evaluate(enriched, criteria)

    row = CompanyQualification(
        candidate_id=candidate_id,
        research_id=evaluation.research_id,
        rule_version=evaluation.rule_version,
        criteria=criteria.model_dump(mode="json"),
        status=evaluation.status,
        score=evaluation.score,
        threshold=evaluation.threshold,
        evidence_coverage=evaluation.evidence_coverage,
        factors=[factor.model_dump(mode="json") for factor in evaluation.factors],
        rationale=evaluation.rationale,
        qualified_at=now or datetime.now(UTC),
    )
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("Qualification persistence failed for candidate %s", candidate_id)
        raise QualificationPersistenceError(
            "Qualification results could not be saved."
        ) from exc

    logger.info(
        "Qualified candidate %s: %s (score %s, rules %s)",
        candidate_id,
        row.status,
        row.score,
        row.rule_version,
    )
    return row


def _newest_first(candidate_id: int) -> Select[tuple[CompanyQualification]]:
    return (
        select(CompanyQualification)
        .where(CompanyQualification.candidate_id == candidate_id)
        .order_by(CompanyQualification.qualified_at.desc(), CompanyQualification.id.desc())
    )


def latest_qualification(
    session: Session, candidate_id: int
) -> CompanyQualification | None:
    return session.execute(_newest_first(candidate_id).limit(1)).scalar_one_or_none()


def list_qualifications(
    session: Session, candidate_id: int, *, limit: int
) -> tuple[int, list[CompanyQualification]]:
    """Return the total run count and up to ``limit`` runs, newest first.

    Raises :class:`CandidateNotFoundError` for an unknown candidate.
    """
    get_candidate(session, candidate_id)
    total = session.execute(
        select(func.count())
        .select_from(CompanyQualification)
        .where(CompanyQualification.candidate_id == candidate_id)
    ).scalar_one()
    rows = list(session.execute(_newest_first(candidate_id).limit(limit)).scalars())
    return total, rows


def is_eligible(row: CompanyQualification) -> bool:
    """Whether a persisted run justifies saving the candidate as a prospect.

    Only runs from :data:`SAVEABLE_RULE_VERSIONS` qualify: v1 could mark a
    candidate ``qualified`` from search-result text alone, so a v1 result
    must be re-run before saving. Existing prospects are not affected.
    """
    return (
        row.rule_version in SAVEABLE_RULE_VERSIONS
        and row.status == "qualified"
        and row.score >= row.threshold
    )


# -- serialization ------------------------------------------------------
def _unique(values: list[str]) -> list[str]:
    """De-duplicate while keeping first-seen order (deterministic)."""
    return list(dict.fromkeys(values))


def to_qualification_record(row: CompanyQualification) -> QualificationRecord:
    factors = [FactorOutcome.model_validate(item) for item in row.factors]
    matched: list[str] = []
    missing: list[str] = []
    disqualifying: list[str] = []
    for factor in factors:
        if factor.key in _POSITIVE_FACTORS:
            matched.extend(factor.signals_found)
            missing.extend(factor.signals_not_found)
        elif factor.disqualifying:
            disqualifying.extend(factor.signals_found)
    matched = _unique(matched)
    # A term found by one factor is not "missing" overall.
    missing = [term for term in _unique(missing) if term not in matched]
    return QualificationRecord(
        id=row.id,
        candidate_id=row.candidate_id,
        research_id=row.research_id,
        rule_version=row.rule_version,
        # Stored criteria were validated when the run was created; the
        # lenient snapshot keeps runs recorded under older rules readable.
        criteria=StoredCriteria.model_validate(row.criteria),
        status=cast(QualificationStatus, row.status),
        score=row.score,
        threshold=row.threshold,
        evidence_coverage=row.evidence_coverage,
        matched_signals=matched,
        missing_signals=missing,
        disqualifying_signals=disqualifying,
        factors=factors,
        rationale=row.rationale,
        qualified_at=row.qualified_at,
        created_at=row.created_at,
    )


def to_qualification_summary(row: CompanyQualification) -> QualificationSummary:
    return QualificationSummary(
        id=row.id,
        status=cast(QualificationStatus, row.status),
        score=row.score,
        threshold=row.threshold,
        evidence_coverage=row.evidence_coverage,
        rule_version=row.rule_version,
        profile=str(row.criteria.get("profile", "")),
        qualified_at=row.qualified_at,
    )
