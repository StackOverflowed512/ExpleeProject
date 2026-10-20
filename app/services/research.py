"""Research service: fetch a candidate website and store provenance.

Policy: ``research_records`` is append-only. Every real state change (a new
success, a new failure after success, or changed page content) creates a new
record, so failures never erase earlier successful research. An identical
consecutive attempt is idempotent: the service returns the existing record
instead of inserting a duplicate. ``attempt_key`` chains the attempt to the
previous record and a result signature, and a unique constraint makes the
same transition safe under concurrency.

The service depends on the :class:`WebsiteFetcher` protocol and owns its
transaction boundary. It never accepts a URL from the caller; research uses
only the URL already stored on the candidate.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.research_record import ResearchRecord
from app.schemas.research import (
    ResearchHistoryResponse,
    ResearchRecordSummary,
    ResearchResponse,
)
from app.services.providers.website import (
    FetchOutcome,
    WebsiteFetcher,
    WebsiteFetchResult,
)

logger = logging.getLogger(__name__)

_SUCCESSFUL_OUTCOMES = (FetchOutcome.SUCCESS.value, FetchOutcome.PARTIAL.value)
_SIGNATURE_SEPARATOR = "\x1f"


class ResearchError(Exception):
    """Base class for research domain errors."""

    code = "research_error"
    http_status = 400


class CandidateNotFoundError(ResearchError):
    """The requested company candidate does not exist."""

    code = "candidate_not_found"
    http_status = 404


class ResearchWebsiteMissingError(ResearchError):
    """The candidate has no stored website URL to research."""

    code = "candidate_website_missing"
    http_status = 422


class ResearchPersistenceError(ResearchError):
    """Research could not be saved; the transaction was rolled back."""

    code = "research_persistence_failed"
    http_status = 500


def require_candidate(
    session: Session, candidate_id: int
) -> CompanyCandidate:
    """Return the candidate or raise :class:`CandidateNotFoundError`."""
    candidate = session.get(CompanyCandidate, candidate_id)
    if candidate is None:
        raise CandidateNotFoundError("Company candidate not found.")
    return candidate


def research_candidate(
    session: Session,
    candidate: CompanyCandidate,
    fetcher: WebsiteFetcher,
) -> ResearchResponse:
    """Fetch, extract, and persist research for ``candidate``.

    A failed fetch is stored as an auditable failed attempt and returned as a
    normal result. Only a persistence failure raises.
    """
    requested_url = (candidate.website_url or "").strip()
    if not requested_url:
        raise ResearchWebsiteMissingError(
            "The company candidate has no website URL to research."
        )

    result = fetcher.fetch(requested_url)
    if result.outcome == FetchOutcome.FAILED:
        logger.info(
            "Research fetch failed for candidate %s: %s",
            candidate.id,
            result.failure_category.value
            if result.failure_category is not None
            else "unknown",
        )

    signature = _result_signature(result)
    latest = _latest_record(session, candidate.id)
    if latest is not None and _record_signature(latest) == signature:
        return _to_response(latest, created=False)

    attempt_key = build_attempt_key(
        candidate.id,
        latest.id if latest is not None else None,
        signature,
    )
    existing = _find_by_attempt_key(session, candidate.id, attempt_key)
    if existing is not None:
        return _to_response(existing, created=False)

    record = ResearchRecord(
        company_candidate_id=candidate.id,
        requested_url=result.requested_url or requested_url,
        final_url=result.final_url,
        fetched_at=result.fetched_at,
        http_status=result.status_code,
        content_type=result.content_type,
        page_title=result.page_title,
        page_text=(
            None if result.outcome == FetchOutcome.FAILED else result.text
        ),
        outcome=result.outcome.value,
        failure_category=(
            result.failure_category.value
            if result.failure_category is not None
            else None
        ),
        truncated=result.truncated,
        content_hash=_content_hash(result),
        attempt_key=attempt_key,
    )

    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        # Another request stored the same state transition first. The
        # savepoint was rolled back, so the winner can be read safely.
        existing = _find_by_attempt_key(session, candidate.id, attempt_key)
        if existing is None:
            session.rollback()
            raise ResearchPersistenceError(
                "Research could not be saved."
            ) from None
        session.commit()
        return _to_response(existing, created=False)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception(
            "Research persistence failed for candidate %s", candidate.id
        )
        raise ResearchPersistenceError(
            "Research could not be saved."
        ) from exc

    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception(
            "Research persistence failed for candidate %s", candidate.id
        )
        raise ResearchPersistenceError(
            "Research could not be saved."
        ) from exc

    return _to_response(record, created=True)


def list_research(
    session: Session, candidate_id: int
) -> list[ResearchRecord]:
    """Return every stored attempt for a candidate, newest first."""
    stmt = (
        select(ResearchRecord)
        .where(ResearchRecord.company_candidate_id == candidate_id)
        .order_by(
            ResearchRecord.fetched_at.desc(), ResearchRecord.id.desc()
        )
    )
    return list(session.execute(stmt).scalars().all())


def latest_successful_research(
    session: Session, candidate_id: int
) -> ResearchRecord | None:
    """Return the newest successful or partial attempt, if any."""
    stmt = (
        select(ResearchRecord)
        .where(
            ResearchRecord.company_candidate_id == candidate_id,
            ResearchRecord.outcome.in_(_SUCCESSFUL_OUTCOMES),
        )
        .order_by(
            ResearchRecord.fetched_at.desc(), ResearchRecord.id.desc()
        )
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_research_history(
    session: Session, candidate_id: int
) -> ResearchHistoryResponse:
    """Return a candidate's research history or raise not-found."""
    require_candidate(session, candidate_id)
    records = list_research(session, candidate_id)
    latest_successful = next(
        (record for record in records if record.outcome in _SUCCESSFUL_OUTCOMES),
        None,
    )
    return ResearchHistoryResponse(
        candidate_id=candidate_id,
        records=[_to_summary(record) for record in records],
        latest_successful=(
            _to_summary(latest_successful)
            if latest_successful is not None
            else None
        ),
    )


def build_attempt_key(
    candidate_id: int,
    previous_record_id: int | None,
    result_signature: str,
) -> str:
    """Return the deterministic idempotency key for a state transition."""
    payload = _SIGNATURE_SEPARATOR.join(
        (
            str(candidate_id),
            str(previous_record_id if previous_record_id is not None else 0),
            result_signature,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_research_summary(record: ResearchRecord) -> ResearchRecordSummary:
    """Convert a stored record to its public representation."""
    return _to_summary(record)


def _latest_record(
    session: Session, candidate_id: int
) -> ResearchRecord | None:
    stmt = (
        select(ResearchRecord)
        .where(ResearchRecord.company_candidate_id == candidate_id)
        .order_by(
            ResearchRecord.fetched_at.desc(), ResearchRecord.id.desc()
        )
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _find_by_attempt_key(
    session: Session, candidate_id: int, attempt_key: str
) -> ResearchRecord | None:
    stmt = select(ResearchRecord).where(
        ResearchRecord.company_candidate_id == candidate_id,
        ResearchRecord.attempt_key == attempt_key,
    )
    return session.execute(stmt).scalar_one_or_none()


def _content_hash(result: WebsiteFetchResult) -> str | None:
    if result.outcome == FetchOutcome.FAILED:
        return None
    payload = _SIGNATURE_SEPARATOR.join(
        (result.page_title or "", result.text)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_signature(result: WebsiteFetchResult) -> str:
    return _signature(
        requested_url=result.requested_url,
        outcome=result.outcome.value,
        failure_category=(
            result.failure_category.value
            if result.failure_category is not None
            else ""
        ),
        http_status=result.status_code,
        final_url=result.final_url,
        content_type=result.content_type,
        content_hash=_content_hash(result),
    )


def _record_signature(record: ResearchRecord) -> str:
    return _signature(
        requested_url=record.requested_url,
        outcome=record.outcome,
        failure_category=record.failure_category or "",
        http_status=record.http_status,
        final_url=record.final_url,
        content_type=record.content_type,
        content_hash=record.content_hash,
    )


def _signature(
    *,
    requested_url: str,
    outcome: str,
    failure_category: str,
    http_status: int | None,
    final_url: str | None,
    content_type: str | None,
    content_hash: str | None,
) -> str:
    payload = _SIGNATURE_SEPARATOR.join(
        (
            requested_url,
            outcome,
            failure_category,
            "" if http_status is None else str(http_status),
            final_url or "",
            content_type or "",
            content_hash or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_summary(record: ResearchRecord) -> ResearchRecordSummary:
    return ResearchRecordSummary(
        id=record.id,
        company_candidate_id=record.company_candidate_id,
        requested_url=record.requested_url,
        final_url=record.final_url,
        fetched_at=record.fetched_at,
        http_status=record.http_status,
        content_type=record.content_type,
        page_title=record.page_title,
        page_text=record.page_text,
        outcome=record.outcome,
        failure_category=record.failure_category,
        truncated=record.truncated,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_response(
    record: ResearchRecord, *, created: bool
) -> ResearchResponse:
    summary = _to_summary(record)
    return ResearchResponse(
        **summary.model_dump(),
        created=created,
    )
