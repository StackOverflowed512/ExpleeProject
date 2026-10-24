"""Company website research and enrichment endpoints.

None of these routes accept a URL. Research always fetches the website
stored on the persisted candidate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import get_session, get_session_factory
from app.errors import AppError
from app.routers.params import CandidateId
from app.schemas.research import (
    EnrichedCompany,
    ResearchHistoryResponse,
    ResearchRequest,
    ResearchRunResponse,
)
from app.services.enrichment import get_enrichment
from app.services.providers.website import WebsiteFetcher
from app.services.research import (
    CandidateNotFoundError,
    CandidateWebsiteMissingError,
    ResearchPersistenceError,
    get_candidate,
    latest_usable_research,
    list_attempts,
    research_status,
    run_research,
    to_record,
    to_summary,
)

router = APIRouter(tags=["research"])

_NOT_FOUND = {404: {"description": "Unknown candidate (error envelope)."}}
_VALIDATION = {422: {"description": "Request validation failed (error envelope)."}}


def _not_found(exc: CandidateNotFoundError) -> AppError:
    return AppError(str(exc), code="candidate_not_found", status_code=404)


@router.post(
    "/companies/{candidate_id}/research",
    response_model=ResearchRunResponse,
    summary="Research a candidate's website",
    description=(
        "Fetches the website URL stored on the candidate (never a "
        "caller-supplied URL) and records the attempt. Fresh usable research "
        "is reused unless `refresh` is true. Fetch failures are not HTTP "
        "errors: the attempt is recorded with outcome `failed` and a safe "
        "`failure_category`, and earlier usable research stays in "
        "`latest_usable`."
    ),
    responses={
        **_NOT_FOUND,
        409: {"description": "Candidate has no website URL (error envelope)."},
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def request_research(
    candidate_id: CandidateId,
    request: Request,
    payload: Annotated[ResearchRequest | None, Body()] = None,
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> ResearchRunResponse:
    fetcher: WebsiteFetcher = request.app.state.website_fetcher
    settings: Settings = request.app.state.settings
    options = payload or ResearchRequest()
    try:
        # The service opens its own short sessions so no session is held
        # across the outbound fetch.
        run = run_research(
            session_factory,
            fetcher,
            candidate_id,
            refresh=options.refresh,
            freshness=timedelta(hours=settings.research_freshness_hours),
        )
    except CandidateNotFoundError as exc:
        raise _not_found(exc) from exc
    except CandidateWebsiteMissingError as exc:
        raise AppError(
            str(exc), code="candidate_website_missing", status_code=409
        ) from exc
    except ResearchPersistenceError as exc:
        raise AppError(
            str(exc), code="research_persistence_failed", status_code=500
        ) from exc

    return ResearchRunResponse(
        candidate_id=run.candidate_id,
        status=run.status,
        reused=run.reused,
        attempt=to_summary(run.attempt),
        latest_usable=to_record(run.latest_usable) if run.latest_usable else None,
    )


@router.get(
    "/companies/{candidate_id}/research",
    response_model=ResearchHistoryResponse,
    summary="Get stored research for a candidate",
    description=(
        "Returns the latest usable research (with extracted text) and the "
        "retained attempt history (metadata only, newest first). Never "
        "fetches."
    ),
    responses={**_NOT_FOUND, **_VALIDATION},
)
def read_research(
    candidate_id: CandidateId,
    session: Session = Depends(get_session),
) -> ResearchHistoryResponse:
    try:
        get_candidate(session, candidate_id)
    except CandidateNotFoundError as exc:
        raise _not_found(exc) from exc

    attempts = list_attempts(session, candidate_id)
    usable = latest_usable_research(session, candidate_id)
    return ResearchHistoryResponse(
        candidate_id=candidate_id,
        status=research_status(usable, attempts[0] if attempts else None),
        latest_usable=to_record(usable) if usable else None,
        attempts=[to_summary(row) for row in attempts],
    )


@router.get(
    "/companies/{candidate_id}/enrichment",
    response_model=EnrichedCompany,
    tags=["enrichment"],
    summary="Get the enriched candidate",
    description=(
        "Combines the candidate's discovery fields and provenance with its "
        "latest usable website research. Read-only and deterministic; "
        "unknown values are null and no facts are inferred from page text."
    ),
    responses={**_NOT_FOUND, **_VALIDATION},
)
def read_enrichment(
    candidate_id: CandidateId,
    session: Session = Depends(get_session),
) -> EnrichedCompany:
    try:
        return get_enrichment(session, candidate_id)
    except CandidateNotFoundError as exc:
        raise _not_found(exc) from exc
