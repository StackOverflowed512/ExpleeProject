"""Overview: real counts, the review queue, recent deliveries, next steps.

Read-only. Every figure comes from an existing service query; nothing is
estimated or invented.
"""

from __future__ import annotations

from typing import get_args

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session
from app.schemas.drafts import DraftState
from app.schemas.prospects import ProspectQuery
from app.services.campaigns import list_campaigns, to_campaign_summary
from app.services.delivery import list_attempts_overview
from app.services.discovery import list_candidates
from app.services.drafts import count_drafts_by_state, list_drafts_overview
from app.services.prospects import list_prospects
from app.web import presentation
from app.web.rendering import render

router = APIRouter()

DRAFT_STATES: tuple[str, ...] = get_args(DraftState)


@router.get("/overview")
def overview_page(request: Request, session: Session = Depends(get_session)) -> Response:
    candidates, _ = list_candidates(session, limit=1)
    prospects, _ = list_prospects(session, ProspectQuery(limit=1))
    campaigns_total, campaign_rows = list_campaigns(session, limit=5, offset=0)
    drafts_by_state = {state: count_drafts_by_state(session, (state,)) for state in DRAFT_STATES}
    _, review_queue = list_drafts_overview(session, states=("pending",), limit=5)
    attempts_total, recent_attempts = list_attempts_overview(
        session, outcome=None, mode=None, limit=6
    )
    return render(
        request,
        "overview/index.html",
        {
            "counts": {
                "candidates": candidates,
                "prospects": prospects,
                "campaigns": campaigns_total,
                "attempts": attempts_total,
            },
            "drafts_by_state": drafts_by_state,
            "campaigns": [to_campaign_summary(c, count) for c, count in campaign_rows],
            "review_queue": review_queue,
            "recent_attempts": recent_attempts,
            "next_steps": presentation.next_steps(
                candidates=candidates,
                prospects=prospects,
                campaigns=campaigns_total,
                drafts_by_state=drafts_by_state,
            ),
        },
        nav="overview",
    )
