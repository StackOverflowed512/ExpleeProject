"""Delivery history: every audited send attempt across drafts (read-only)."""

from __future__ import annotations

from typing import get_args

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session
from app.routers.ui.common import PAGE_SIZE, Offset, page_links
from app.schemas.drafts import AttemptMode, AttemptOutcome
from app.services.delivery import list_attempts_overview
from app.web.rendering import render, wants_fragment

router = APIRouter()

OUTCOMES = get_args(AttemptOutcome)
MODES = get_args(AttemptMode)


@router.get("/deliveries")
def deliveries_page(
    request: Request,
    outcome: str = "",
    mode: str = "",
    offset: Offset = 0,
    session: Session = Depends(get_session),
) -> Response:
    # Unknown filter values are ignored rather than passed to the query.
    selected_outcome = outcome if outcome in OUTCOMES else ""
    selected_mode = mode if mode in MODES else ""
    total, items = list_attempts_overview(
        session,
        outcome=selected_outcome or None,
        mode=selected_mode or None,
        limit=PAGE_SIZE,
        offset=offset,
    )
    context = {
        "items": items,
        "pages": page_links(total, offset),
        "outcomes": OUTCOMES,
        "modes": MODES,
        "outcome": selected_outcome,
        "mode": selected_mode,
    }
    if wants_fragment(request, "delivery-results"):
        return render(request, "deliveries/_results.html", context)
    return render(request, "deliveries/index.html", context, nav="deliveries")
