"""Saved prospects: eligibility-gated saving, search, and retrieval.

Saving rules (enforced here, not only in request validation):

1. The candidate must exist.
2. If the candidate is already saved, the existing prospect is returned
   unchanged (``created=False``). Saving is idempotent; the justifying
   qualification is never replaced by a later save request.
3. Otherwise a persisted qualification is required: the one named by
   ``qualification_id`` (which must belong to the candidate) or, when
   omitted, the candidate's latest run.
4. That qualification must come from a current rule version
   (``SAVEABLE_RULE_VERSIONS``), have status ``qualified`` (not
   ``preliminary``), and have a score at or above its recorded threshold.

The unique constraint on ``saved_prospects.candidate_id`` is the final
guard: inserts use ``ON CONFLICT DO NOTHING``, so a concurrent save of the
same candidate resolves to the row that won instead of creating a duplicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.saved_prospect import SavedProspect
from app.schemas.discovery import CandidateSummary
from app.schemas.prospects import ProspectDetail, ProspectQuery, ProspectSummary
from app.services.qualification import (
    SAVEABLE_RULE_VERSIONS,
    is_eligible,
    latest_qualification,
    to_qualification_record,
    to_qualification_summary,
)
from app.services.research import get_candidate

logger = logging.getLogger(__name__)

ProspectRow = tuple[SavedProspect, CompanyCandidate, CompanyQualification]


class QualificationRequiredError(Exception):
    """The candidate has no persisted qualification to justify saving."""


class QualificationNotFoundError(Exception):
    """The named qualification does not exist for this candidate."""


class ProspectNotQualifiedError(Exception):
    """The qualification does not meet its threshold or is not 'qualified'."""


class ProspectNotFoundError(Exception):
    """No saved prospect exists with the requested ID."""


class ProspectPersistenceError(Exception):
    """A prospect could not be saved."""


@dataclass(frozen=True)
class SaveResult:
    prospect: SavedProspect
    candidate: CompanyCandidate
    qualification: CompanyQualification
    created: bool


def _existing(session: Session, candidate_id: int) -> SavedProspect | None:
    return session.execute(
        select(SavedProspect).where(SavedProspect.candidate_id == candidate_id)
    ).scalar_one_or_none()


def _result(
    session: Session, prospect: SavedProspect, candidate: CompanyCandidate, created: bool
) -> SaveResult:
    qualification = session.get(CompanyQualification, prospect.qualification_id)
    if qualification is None:  # pragma: no cover - guarded by the foreign key
        raise ProspectPersistenceError("A saved prospect lost its qualification.")
    return SaveResult(prospect, candidate, qualification, created)


def _justifying_qualification(
    session: Session, candidate_id: int, qualification_id: int | None
) -> CompanyQualification:
    if qualification_id is None:
        qualification = latest_qualification(session, candidate_id)
        if qualification is None:
            raise QualificationRequiredError(
                f"Company candidate {candidate_id} has not been qualified; "
                "run qualification before saving."
            )
    else:
        qualification = session.get(CompanyQualification, qualification_id)
        if qualification is None or qualification.candidate_id != candidate_id:
            raise QualificationNotFoundError(
                f"Qualification {qualification_id} was not found for "
                f"company candidate {candidate_id}."
            )
    if not is_eligible(qualification):
        if qualification.rule_version not in SAVEABLE_RULE_VERSIONS:
            hint = (
                f"it was produced by {qualification.rule_version}; re-run qualification "
                f"to apply {', '.join(sorted(SAVEABLE_RULE_VERSIONS))}"
            )
        elif qualification.status == "preliminary":
            hint = "research the company's website and re-run qualification to confirm it"
        else:
            hint = "only a qualified result can be saved"
        raise ProspectNotQualifiedError(
            f"Qualification {qualification.id} has status '{qualification.status}' "
            f"(score {qualification.score}, threshold {qualification.threshold}); {hint}."
        )
    return qualification


def save_prospect(
    session: Session, candidate_id: int, *, qualification_id: int | None = None
) -> SaveResult:
    """Save a qualified candidate as a prospect, or return the existing one."""
    candidate = get_candidate(session, candidate_id)
    existing = _existing(session, candidate_id)
    if existing is not None:
        return _result(session, existing, candidate, created=False)

    qualification = _justifying_qualification(session, candidate_id, qualification_id)
    stmt = (
        pg_insert(SavedProspect)
        .values(candidate_id=candidate_id, qualification_id=qualification.id)
        .on_conflict_do_nothing(index_elements=["candidate_id"])
        .returning(SavedProspect.id)
    )
    try:
        inserted_id = session.execute(stmt).scalar_one_or_none()
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("Prospect persistence failed for candidate %s", candidate_id)
        raise ProspectPersistenceError("The prospect could not be saved.") from exc

    if inserted_id is None:
        # A concurrent request saved this candidate first; return its row.
        winner = _existing(session, candidate_id)
        if winner is None:  # pragma: no cover - defensive
            raise ProspectPersistenceError("A conflicting prospect could not be located.")
        return _result(session, winner, candidate, created=False)

    prospect = session.get(SavedProspect, inserted_id)
    if prospect is None:  # pragma: no cover - defensive
        raise ProspectPersistenceError("A saved prospect could not be read back.")
    logger.info(
        "Saved candidate %s as prospect %s (qualification %s)",
        candidate_id,
        prospect.id,
        qualification.id,
    )
    return SaveResult(prospect, candidate, qualification, created=True)


# -- queries ------------------------------------------------------------
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_query() -> Select[tuple[SavedProspect, CompanyCandidate, CompanyQualification]]:
    return (
        select(SavedProspect, CompanyCandidate, CompanyQualification)
        .join(CompanyCandidate, SavedProspect.candidate_id == CompanyCandidate.id)
        .join(CompanyQualification, SavedProspect.qualification_id == CompanyQualification.id)
    )


def _filtered(
    stmt: Select[tuple[SavedProspect, CompanyCandidate, CompanyQualification]],
    query: ProspectQuery,
) -> Select[tuple[SavedProspect, CompanyCandidate, CompanyQualification]]:
    if query.q is not None:
        pattern = f"%{_escape_like(query.q)}%"
        stmt = stmt.where(
            CompanyCandidate.name.ilike(pattern, escape="\\")
            | CompanyCandidate.domain.ilike(pattern, escape="\\")
            | CompanyCandidate.description.ilike(pattern, escape="\\")
        )
    if query.domain is not None:
        stmt = stmt.where(CompanyCandidate.domain == query.domain)
    if query.min_score is not None:
        stmt = stmt.where(CompanyQualification.score >= query.min_score)
    if query.max_score is not None:
        stmt = stmt.where(CompanyQualification.score <= query.max_score)
    if query.saved_after is not None:
        stmt = stmt.where(SavedProspect.saved_at >= query.saved_after)
    if query.saved_before is not None:
        stmt = stmt.where(SavedProspect.saved_at < query.saved_before)
    return stmt


_ORDERINGS = {
    "saved_at_desc": (SavedProspect.saved_at.desc(), SavedProspect.id.desc()),
    "saved_at_asc": (SavedProspect.saved_at.asc(), SavedProspect.id.asc()),
    "score_desc": (
        CompanyQualification.score.desc(),
        SavedProspect.saved_at.desc(),
        SavedProspect.id.desc(),
    ),
    "name_asc": (func.lower(CompanyCandidate.name).asc(), SavedProspect.id.asc()),
}


def list_prospects(session: Session, query: ProspectQuery) -> tuple[int, list[ProspectRow]]:
    """Return the number of matching prospects and one ordered page."""
    filtered = _filtered(_base_query(), query)
    total = session.execute(
        select(func.count()).select_from(filtered.subquery())
    ).scalar_one()
    page = filtered.order_by(*_ORDERINGS[query.sort]).limit(query.limit).offset(query.offset)
    rows = [(p, c, q) for p, c, q in session.execute(page).tuples()]
    return total, rows


def get_prospect(session: Session, prospect_id: int) -> ProspectRow:
    row = session.execute(
        _base_query().where(SavedProspect.id == prospect_id)
    ).tuples().one_or_none()
    if row is None:
        raise ProspectNotFoundError(f"Saved prospect {prospect_id} was not found.")
    return row[0], row[1], row[2]


# -- serialization ------------------------------------------------------
def to_prospect_summary(
    prospect: SavedProspect, candidate: CompanyCandidate, qualification: CompanyQualification
) -> ProspectSummary:
    return ProspectSummary(
        id=prospect.id,
        candidate_id=candidate.id,
        name=candidate.name,
        domain=candidate.domain,
        website_url=candidate.website_url,
        saved_at=prospect.saved_at,
        updated_at=prospect.updated_at,
        qualification=to_qualification_summary(qualification),
    )


def to_prospect_detail(
    prospect: SavedProspect, candidate: CompanyCandidate, qualification: CompanyQualification
) -> ProspectDetail:
    return ProspectDetail(
        id=prospect.id,
        saved_at=prospect.saved_at,
        updated_at=prospect.updated_at,
        candidate=CandidateSummary.model_validate(candidate, from_attributes=True),
        qualification=to_qualification_record(qualification),
    )


def find_saved_prospect(session: Session, candidate_id: int) -> SavedProspect | None:
    """The candidate's saved prospect, if it has been saved (read-only)."""
    return _existing(session, candidate_id)
