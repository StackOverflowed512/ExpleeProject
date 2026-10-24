"""Company qualification endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AppError
from app.routers.params import CandidateId
from app.schemas.qualification import (
    QualificationHistoryResponse,
    QualificationRecord,
    QualifyRequest,
)
from app.services.qualification import (
    QualificationPersistenceError,
    list_qualifications,
    run_qualification,
    to_qualification_record,
)
from app.services.research import CandidateNotFoundError

router = APIRouter(tags=["qualification"])

_NOT_FOUND = {404: {"description": "Unknown candidate (error envelope)."}}
_VALIDATION = {422: {"description": "Invalid criteria or parameters (error envelope)."}}


def _not_found(exc: CandidateNotFoundError) -> AppError:
    return AppError(str(exc), code="candidate_not_found", status_code=404)


@router.post(
    "/companies/{candidate_id}/qualify",
    response_model=QualificationRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Qualify a candidate",
    description=(
        "Applies the deterministic, versioned qualification rules to the "
        "candidate's stored discovery data and latest usable website research, "
        "and records a new run (earlier runs are never modified). No network "
        "requests are made; run research first for stronger evidence. Omitted "
        "criteria use the documented defaults."
    ),
    responses={
        **_NOT_FOUND,
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def qualify(
    candidate_id: CandidateId,
    payload: Annotated[QualifyRequest | None, Body()] = None,
    session: Session = Depends(get_session),
) -> QualificationRecord:
    request = payload or QualifyRequest()
    try:
        row = run_qualification(session, candidate_id, request.criteria)
    except CandidateNotFoundError as exc:
        raise _not_found(exc) from exc
    except QualificationPersistenceError as exc:
        raise AppError(
            str(exc), code="qualification_persistence_failed", status_code=500
        ) from exc
    return to_qualification_record(row)


@router.get(
    "/companies/{candidate_id}/qualifications",
    response_model=QualificationHistoryResponse,
    summary="Get a candidate's qualification history",
    description="Returns stored qualification runs, newest first, with factors and rationale.",
    responses={**_NOT_FOUND, **_VALIDATION},
)
def read_qualifications(
    candidate_id: CandidateId,
    limit: Annotated[int, Query(ge=1, le=100, description="Runs to return.")] = 20,
    session: Session = Depends(get_session),
) -> QualificationHistoryResponse:
    try:
        total, rows = list_qualifications(session, candidate_id, limit=limit)
    except CandidateNotFoundError as exc:
        raise _not_found(exc) from exc
    history = [to_qualification_record(row) for row in rows]
    return QualificationHistoryResponse(
        candidate_id=candidate_id,
        total=total,
        latest=history[0] if history else None,
        history=history,
    )
