"""Read access to individual outreach drafts (no approval or sending)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AppError
from app.routers.params import DraftId
from app.schemas.drafts import DraftRecord
from app.services.drafts import DraftNotFoundError, get_draft, to_draft_record

router = APIRouter(tags=["drafts"])


@router.get(
    "/drafts/{draft_id}",
    response_model=DraftRecord,
    summary="Get an outreach draft",
    description="Returns one draft with its audit trail (prompt version, model, provenance).",
    responses={
        404: {"description": "Unknown draft (error envelope)."},
        422: {"description": "Invalid draft ID (error envelope)."},
    },
)
def read_draft(
    draft_id: DraftId,
    session: Session = Depends(get_session),
) -> DraftRecord:
    try:
        return to_draft_record(get_draft(session, draft_id))
    except DraftNotFoundError as exc:
        raise AppError(str(exc), code="draft_not_found", status_code=404) from exc
