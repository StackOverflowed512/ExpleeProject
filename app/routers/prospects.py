"""Saved prospect endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AppError
from app.routers.params import CandidateId, ProspectId
from app.schemas.prospects import (
    ProspectDetail,
    ProspectListResponse,
    ProspectQuery,
    SaveProspectRequest,
    SaveProspectResponse,
)
from app.services.prospects import (
    ProspectNotFoundError,
    ProspectNotQualifiedError,
    ProspectPersistenceError,
    QualificationNotFoundError,
    QualificationRequiredError,
    get_prospect,
    list_prospects,
    save_prospect,
    to_prospect_detail,
    to_prospect_summary,
)
from app.services.research import CandidateNotFoundError

router = APIRouter(tags=["prospects"])


@router.post(
    "/companies/{candidate_id}/save",
    response_model=SaveProspectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a qualified candidate as a prospect",
    description=(
        "Saves the candidate using a persisted qualification with status "
        "'qualified' (the latest run unless `qualification_id` is given). "
        "Returns 201 with `created: true` for a new prospect, or 200 with "
        "`created: false` and the unchanged existing prospect if the candidate "
        "is already saved. Eligibility is enforced by the backend."
    ),
    responses={
        200: {"description": "Candidate already saved; existing prospect returned."},
        404: {"description": "Unknown candidate or qualification (error envelope)."},
        409: {
            "description": (
                "No qualification yet, or the qualification is not 'qualified' "
                "(error envelope)."
            )
        },
        422: {"description": "Request validation failed (error envelope)."},
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def save(
    candidate_id: CandidateId,
    response: Response,
    payload: Annotated[SaveProspectRequest | None, Body()] = None,
    session: Session = Depends(get_session),
) -> SaveProspectResponse:
    options = payload or SaveProspectRequest()
    try:
        result = save_prospect(
            session, candidate_id, qualification_id=options.qualification_id
        )
    except CandidateNotFoundError as exc:
        raise AppError(str(exc), code="candidate_not_found", status_code=404) from exc
    except QualificationNotFoundError as exc:
        raise AppError(str(exc), code="qualification_not_found", status_code=404) from exc
    except QualificationRequiredError as exc:
        raise AppError(str(exc), code="qualification_required", status_code=409) from exc
    except ProspectNotQualifiedError as exc:
        raise AppError(str(exc), code="prospect_not_qualified", status_code=409) from exc
    except ProspectPersistenceError as exc:
        raise AppError(str(exc), code="prospect_persistence_failed", status_code=500) from exc

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return SaveProspectResponse(
        created=result.created,
        prospect=to_prospect_summary(result.prospect, result.candidate, result.qualification),
    )


@router.get(
    "/prospects",
    response_model=ProspectListResponse,
    summary="Search saved prospects",
    description=(
        "Lists saved prospects with optional text search, domain and score "
        "filters, and a saved-time window. Ordering is deterministic (ties "
        "broken by prospect ID) and pagination is bounded (limit <= 100). "
        "Unknown query parameters are rejected."
    ),
    responses={422: {"description": "Invalid filters (error envelope)."}},
)
def search_prospects(
    query: Annotated[ProspectQuery, Query()],
    session: Session = Depends(get_session),
) -> ProspectListResponse:
    total, rows = list_prospects(session, query)
    return ProspectListResponse(
        items=[to_prospect_summary(*row) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
        sort=query.sort,
    )


@router.get(
    "/prospects/{prospect_id}",
    response_model=ProspectDetail,
    summary="Get a saved prospect",
    description="Returns the prospect, its candidate, and the full justifying qualification.",
    responses={
        404: {"description": "Unknown prospect (error envelope)."},
        422: {"description": "Invalid prospect ID (error envelope)."},
    },
)
def read_prospect(
    prospect_id: ProspectId,
    session: Session = Depends(get_session),
) -> ProspectDetail:
    try:
        row = get_prospect(session, prospect_id)
    except ProspectNotFoundError as exc:
        raise AppError(str(exc), code="prospect_not_found", status_code=404) from exc
    return to_prospect_detail(*row)
