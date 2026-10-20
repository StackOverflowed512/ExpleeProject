"""Thin HTTP routes for website research and enrichment."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AppError
from app.schemas.enrichment import EnrichmentResponse
from app.schemas.research import (
    ResearchHistoryResponse,
    ResearchRequest,
    ResearchResponse,
)
from app.services.enrichment import enrich_candidate
from app.services.providers.website import WebsiteFetcher
from app.services.research import (
    ResearchError,
    get_research_history,
    require_candidate,
    research_candidate,
)

router = APIRouter(tags=["research"])

CandidateID = Annotated[
    int,
    Path(
        ge=1,
        description="ID of a company candidate returned by discovery.",
    ),
]

_CANDIDATE_RESPONSES = {
    404: {"description": "Company candidate not found (error envelope)."},
}


def _raise_app_error(exc: ResearchError) -> None:
    raise AppError(
        str(exc), code=exc.code, status_code=exc.http_status
    ) from exc


@router.post(
    "/companies/{candidate_id}/research",
    response_model=ResearchResponse,
    summary="Research a company website",
    description=(
        "Fetches the website URL stored on the company candidate, extracts "
        "visible text, and stores an append-only research record with its "
        "source URL and fetch timestamp. Client-supplied URLs are not "
        "accepted. A failed or partial fetch is returned as a documented "
        "outcome rather than erasing earlier successful research."
    ),
    responses={
        404: {"description": "Company candidate not found (error envelope)."},
        422: {
            "description": (
                "Invalid candidate ID, unsupported request body, or missing "
                "candidate website URL (error envelope)."
            )
        },
        500: {"description": "Research persistence failure (error envelope)."},
    },
)
def research_company(
    candidate_id: CandidateID,
    request: Request,
    payload: ResearchRequest | None = Body(default=None),
    session: Session = Depends(get_session),
) -> ResearchResponse:
    del payload  # The body is validated only to reject unexpected fields.
    fetcher: WebsiteFetcher = request.app.state.website_fetcher
    try:
        candidate = require_candidate(session, candidate_id)
        return research_candidate(session, candidate, fetcher)
    except ResearchError as exc:
        _raise_app_error(exc)


@router.get(
    "/companies/{candidate_id}/research",
    response_model=ResearchHistoryResponse,
    summary="Get stored company research",
    description=(
        "Returns every stored research attempt for the candidate, newest "
        "first, including failures. `latest_successful` is the newest "
        "successful or partial attempt and is never removed by a later "
        "failure."
    ),
    responses=_CANDIDATE_RESPONSES,
)
def read_company_research(
    candidate_id: CandidateID,
    session: Session = Depends(get_session),
) -> ResearchHistoryResponse:
    try:
        return get_research_history(session, candidate_id)
    except ResearchError as exc:
        _raise_app_error(exc)


@router.get(
    "/companies/{candidate_id}/enrichment",
    response_model=EnrichmentResponse,
    summary="Enrich a discovered company",
    description=(
        "Combines the original discovery data with stored research. This is "
        "a read-only projection: it never fetches the website and is "
        "idempotent for the same candidate and research history. A failed "
        "latest attempt is reported as `stale` when earlier research exists."
    ),
    responses=_CANDIDATE_RESPONSES,
)
def read_company_enrichment(
    candidate_id: CandidateID,
    session: Session = Depends(get_session),
) -> EnrichmentResponse:
    try:
        return enrich_candidate(session, candidate_id)
    except ResearchError as exc:
        _raise_app_error(exc)
