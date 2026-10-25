"""Saved prospects: filterable list and detail with research history."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session
from app.routers.params import ProspectId
from app.routers.ui.common import PAGE_SIZE, page_links
from app.schemas.prospects import ProspectQuery
from app.services.prospects import (
    ProspectNotFoundError,
    get_prospect,
    list_prospects,
    to_prospect_detail,
    to_prospect_summary,
)
from app.services.research import list_attempts, to_summary
from app.web.forms import field_errors
from app.web.rendering import render, render_error_page, wants_fragment

router = APIRouter()

FILTER_FIELDS = ("q", "domain", "min_score", "max_score", "saved_after", "saved_before", "sort")
SORT_OPTIONS = (
    ("saved_at_desc", "Newest saved"),
    ("saved_at_asc", "Oldest saved"),
    ("score_desc", "Highest score"),
    ("name_asc", "Name (A–Z)"),
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _query_values(request: Request) -> dict[str, str]:
    return {name: request.query_params.get(name, "").strip() for name in FILTER_FIELDS}


def _to_query(values: dict[str, str], offset: str) -> dict[str, Any]:
    """Browser filter values -> ``ProspectQuery`` input (blank = not set)."""
    data: dict[str, Any] = {name: value for name, value in values.items() if value}
    for name in ("saved_after", "saved_before"):
        if name in data and _DATE.match(data[name]):
            # A date input means midnight UTC on that day.
            data[name] = f"{data[name]}T00:00:00+00:00"
    data["limit"] = PAGE_SIZE
    data["offset"] = offset or "0"
    return data


@router.get("/prospects")
def prospects_page(request: Request, session: Session = Depends(get_session)) -> Response:
    values = _query_values(request)
    context: dict[str, Any] = {
        "filters": values,
        "sort_options": SORT_OPTIONS,
        "errors": {},
        "items": [],
        "pages": page_links(0, 0),
        "filtered": any(values[name] for name in FILTER_FIELDS if name != "sort"),
    }
    status_code = 200
    try:
        query = ProspectQuery.model_validate(
            _to_query(values, request.query_params.get("offset", ""))
        )
    except ValidationError as exc:
        context["errors"] = field_errors(exc)
        status_code = 422
    else:
        total, rows = list_prospects(session, query)
        context["items"] = [to_prospect_summary(*row) for row in rows]
        context["pages"] = page_links(total, query.offset)
    if wants_fragment(request, "prospect-results"):
        return render(request, "prospects/_results.html", context, status_code=status_code)
    return render(
        request, "prospects/index.html", context, status_code=status_code, nav="prospects"
    )


@router.get("/prospects/{prospect_id}")
def prospect_page(
    request: Request, prospect_id: ProspectId, session: Session = Depends(get_session)
) -> Response:
    try:
        row = get_prospect(session, prospect_id)
    except ProspectNotFoundError as exc:
        return render_error_page(request, 404, "prospect_not_found", str(exc))
    detail = to_prospect_detail(*row)
    attempts = [to_summary(r) for r in list_attempts(session, detail.candidate.id)]
    return render(
        request,
        "prospects/detail.html",
        {"prospect": detail, "attempts": attempts},
        nav="prospects",
    )
