"""Explorer research: fetch a candidate's stored website and persist the result.

The service depends on the :class:`WebsiteFetcher` protocol only. The fetch
target is always ``CompanyCandidate.website_url``; there is no code path
that fetches a caller-supplied URL.

Policies
--------
* **Freshness.** Unless ``refresh`` is requested, usable research
  (``success`` or ``partial``) fetched within ``freshness`` is returned
  without a network call. ``freshness=0`` disables reuse.
* **History.** Every attempt is a new ``company_research`` row, so a failed
  attempt never erases earlier research. After each insert the candidate's
  history is pruned to the :data:`HISTORY_LIMIT` most recent attempts plus
  the latest usable record, which bounds growth.
* **Provenance.** Discovery fields on the candidate (``source_url``,
  ``provider``, ``discovered_at``) are never modified by research.
* **Session lifecycle.** :func:`run_research` takes a session factory, not
  a caller's session, and runs three isolated steps: a short read-only
  session (candidate and freshness check), the network fetch with **no**
  session or transaction open, and one narrow write transaction (insert
  plus pruning). It never commits or rolls back anyone else's work.
* **Transactions.** The write transaction either persists the attempt and
  the pruning together or rolls both back and raises
  :class:`ResearchPersistenceError`, leaving prior research intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import Select, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.models.company_research import CompanyResearch
from app.schemas.research import (
    ResearchAttemptSummary,
    ResearchRecord,
    ResearchStatus,
)
from app.services.providers.url_safety import UnsafeUrlError, check_url
from app.services.providers.website import (
    USABLE_OUTCOMES,
    FetchFailure,
    FetchOutcome,
    FetchResult,
    WebsiteFetcher,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 5
_USABLE_VALUES = tuple(outcome.value for outcome in USABLE_OUTCOMES)
_MAX_CONTENT_TYPE_LENGTH = 255
_MAX_TITLE_LENGTH = 500
_MAX_FETCHER_LENGTH = 50


class CandidateNotFoundError(Exception):
    """No candidate exists with the requested ID."""


class CandidateWebsiteMissingError(Exception):
    """The candidate has no stored website URL to research."""


class ResearchPersistenceError(Exception):
    """A research attempt could not be saved."""


@dataclass(frozen=True)
class ResearchRun:
    """Outcome of :func:`run_research`."""

    candidate_id: int
    attempt: CompanyResearch
    reused: bool
    latest_usable: CompanyResearch | None
    status: ResearchStatus


# -- queries -----------------------------------------------------------
def get_candidate(session: Session, candidate_id: int) -> CompanyCandidate:
    candidate = session.get(CompanyCandidate, candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"Company candidate {candidate_id} was not found.")
    return candidate


def _newest_first(candidate_id: int) -> Select[tuple[CompanyResearch]]:
    return (
        select(CompanyResearch)
        .where(CompanyResearch.candidate_id == candidate_id)
        .order_by(CompanyResearch.fetched_at.desc(), CompanyResearch.id.desc())
    )


def latest_usable_research(
    session: Session, candidate_id: int
) -> CompanyResearch | None:
    stmt = _newest_first(candidate_id).where(
        CompanyResearch.outcome.in_(_USABLE_VALUES)
    )
    return session.execute(stmt.limit(1)).scalar_one_or_none()


def list_attempts(session: Session, candidate_id: int) -> list[CompanyResearch]:
    return list(session.execute(_newest_first(candidate_id)).scalars())


def research_status(
    latest_usable: CompanyResearch | None,
    latest_attempt: CompanyResearch | None,
) -> ResearchStatus:
    """Summarize stored research for API responses."""
    if latest_usable is not None:
        return "partial" if latest_usable.outcome == FetchOutcome.PARTIAL else "available"
    if latest_attempt is not None:
        return "unavailable"
    return "not_researched"


# -- serialization ------------------------------------------------------
def to_summary(row: CompanyResearch) -> ResearchAttemptSummary:
    return ResearchAttemptSummary.model_validate(row, from_attributes=True)


def to_record(row: CompanyResearch) -> ResearchRecord:
    return ResearchRecord.model_validate(row, from_attributes=True)


# -- research -----------------------------------------------------------
def _host_for_logs(url: str) -> str:
    try:
        return urlsplit(url).hostname or "<none>"
    except ValueError:
        return "<invalid>"


def _rejected_before_fetch(
    url: str, fetcher: WebsiteFetcher, failure: FetchFailure, now: datetime
) -> FetchResult:
    return FetchResult(
        requested_url=url,
        fetched_at=now,
        outcome=FetchOutcome.FAILED,
        fetcher=fetcher.name,
        failure=failure,
    )


def _to_row(candidate_id: int, result: FetchResult) -> CompanyResearch:
    outcome = FetchOutcome(result.outcome)
    failure: str | None = None
    if outcome == FetchOutcome.FAILED:
        # The check constraint requires a category for failures; an adapter
        # that omits one is reported as a malformed response.
        failure = (result.failure or FetchFailure.MALFORMED_RESPONSE).value
    return CompanyResearch(
        candidate_id=candidate_id,
        requested_url=result.requested_url,
        final_url=result.final_url,
        fetched_at=result.fetched_at,
        fetcher=result.fetcher[:_MAX_FETCHER_LENGTH],
        outcome=outcome.value,
        failure_category=failure,
        http_status=result.http_status,
        content_type=(
            result.content_type[:_MAX_CONTENT_TYPE_LENGTH] if result.content_type else None
        ),
        page_title=result.page_title[:_MAX_TITLE_LENGTH] if result.page_title else None,
        extracted_text=result.text if outcome in USABLE_OUTCOMES else None,
        content_truncated=result.content_truncated,
    )


def _prune_history(session: Session, candidate_id: int) -> None:
    ids = list(
        session.execute(
            select(CompanyResearch.id)
            .where(CompanyResearch.candidate_id == candidate_id)
            .order_by(CompanyResearch.fetched_at.desc(), CompanyResearch.id.desc())
        ).scalars()
    )
    keep = set(ids[:HISTORY_LIMIT])
    usable = latest_usable_research(session, candidate_id)
    if usable is not None:
        keep.add(usable.id)
    stale = [row_id for row_id in ids if row_id not in keep]
    if stale:
        session.execute(
            delete(CompanyResearch).where(CompanyResearch.id.in_(stale))
        )


def run_research(
    session_factory: sessionmaker[Session],
    fetcher: WebsiteFetcher,
    candidate_id: int,
    *,
    refresh: bool = False,
    freshness: timedelta = timedelta(hours=24),
    now: datetime | None = None,
) -> ResearchRun:
    """Research the stored website of one candidate.

    Raises :class:`CandidateNotFoundError` for an unknown ID and
    :class:`CandidateWebsiteMissingError` when the candidate has no website;
    neither makes a network request. A stored URL that fails validation is
    recorded as a failed attempt without contacting the network. Fetch
    failures are recorded, never raised. The returned ORM rows are detached
    with their attributes loaded.
    """
    current_time = now or datetime.now(UTC)

    # 1. Read-only session; closed (and its transaction ended) before any
    #    network I/O.
    with session_factory() as session:
        candidate = get_candidate(session, candidate_id)
        website_url = candidate.website_url
        if website_url is None or not website_url.strip():
            raise CandidateWebsiteMissingError(
                f"Company candidate {candidate_id} has no website URL to research."
            )
        if not refresh and freshness > timedelta(0):
            usable = latest_usable_research(session, candidate_id)
            if usable is not None and usable.fetched_at >= current_time - freshness:
                return ResearchRun(
                    candidate_id=candidate_id,
                    attempt=usable,
                    reused=True,
                    latest_usable=usable,
                    status=research_status(usable, usable),
                )

    # 2. Network fetch with no database session or transaction open.
    try:
        check_url(website_url)
    except UnsafeUrlError as exc:
        result = _rejected_before_fetch(website_url, fetcher, exc.failure, current_time)
    else:
        result = fetcher.fetch(website_url)

    host = _host_for_logs(website_url)
    if result.outcome == FetchOutcome.FAILED:
        logger.warning(
            "Research fetch failed for candidate %s (host %s): %s",
            candidate_id,
            host,
            result.failure.value if result.failure else "unknown",
        )
    else:
        logger.info(
            "Research fetch for candidate %s (host %s): %s",
            candidate_id,
            host,
            FetchOutcome(result.outcome).value,
        )

    # 3. One narrow write transaction: insert the attempt and prune history.
    #    ``session.begin()`` commits on success and rolls back on any error.
    row = _to_row(candidate_id, result)
    with session_factory() as session:
        try:
            with session.begin():
                session.add(row)
                session.flush()
                _prune_history(session, candidate_id)
        except SQLAlchemyError as exc:
            logger.exception(
                "Research persistence failed for candidate %s", candidate_id
            )
            raise ResearchPersistenceError(
                "Research results could not be saved."
            ) from exc

        session.refresh(row)
        latest_usable = latest_usable_research(session, candidate_id)

    return ResearchRun(
        candidate_id=candidate_id,
        attempt=row,
        reused=False,
        latest_usable=latest_usable,
        status=research_status(latest_usable, row),
    )
