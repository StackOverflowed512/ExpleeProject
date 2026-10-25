"""Explorer: discovery search, company detail, research, qualification, save."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session, get_session_factory
from app.routers.params import CandidateId
from app.routers.ui.common import PAGE_SIZE, Offset, handle_post, page_links, settings_of
from app.schemas.discovery import DiscoveryRequest, DiscoveryResponse
from app.schemas.qualification import QualificationCriteria
from app.services.discovery import DiscoveryPersistenceError, list_candidates, run_discovery
from app.services.enrichment import get_enrichment
from app.services.prospects import (
    ProspectNotQualifiedError,
    ProspectPersistenceError,
    QualificationNotFoundError,
    QualificationRequiredError,
    find_saved_prospect,
    save_prospect,
)
from app.services.providers.errors import SearchProviderError
from app.services.qualification import (
    QualificationPersistenceError,
    list_qualifications,
    run_qualification,
    to_qualification_record,
)
from app.services.research import (
    CandidateNotFoundError,
    CandidateWebsiteMissingError,
    ResearchPersistenceError,
    list_attempts,
    run_research,
    to_summary,
)
from app.web.forms import FormData, field_errors, split_terms
from app.web.presentation import research_notice
from app.web.rendering import redirect, render, render_error_page, wants_fragment

router = APIRouter()

SEARCH_FIELDS = ("query", "limit")
QUALIFY_FIELDS = (
    "profile",
    "target_keywords",
    "target_locations",
    "target_tlds",
    "excluded_keywords",
    "threshold",
    "min_evidence_coverage",
)
_PROVIDER_HINTS = {
    "provider_not_configured": (
        "Set TAVILY_API_KEY in .env (or SEARCH_PROVIDER=fake for offline use) and "
        "restart the server."
    ),
    "provider_authentication_failed": "Check that TAVILY_API_KEY is valid.",
    "provider_rate_limited": "Wait a moment and search again.",
    "provider_timeout": "The search provider was slow; try again.",
    "provider_unavailable": "The search provider is unavailable; try again later.",
}


def _explorer_context(
    request: Request, session: Session, offset: int = 0, **extra: Any
) -> dict[str, Any]:
    total, recent = list_candidates(session, limit=PAGE_SIZE, offset=offset)
    context: dict[str, Any] = {
        "recent": recent,
        "pages": page_links(total, offset),
        "search_provider": settings_of(request).search_provider,
        "form": {"query": "", "limit": "5"},
        "errors": {},
        "error": None,
        "error_hint": None,
        "results": None,
    }
    context.update(extra)
    return context


def _explorer_response(
    request: Request, session: Session, *, status_code: int = 200, **extra: Any
) -> Response:
    if wants_fragment(request, "search-results"):
        context = {
            "results": None,
            "errors": {},
            "error": None,
            "error_hint": None,
            "search_provider": settings_of(request).search_provider,
        }
        context.update(extra)
        return render(request, "explorer/_results.html", context, status_code=status_code)
    return render(
        request,
        "explorer/index.html",
        _explorer_context(request, session, **extra),
        status_code=status_code,
        nav="explorer",
    )


@router.get("/explorer")
def explorer_page(
    request: Request, offset: Offset = 0, session: Session = Depends(get_session)
) -> Response:
    return render(
        request, "explorer/index.html", _explorer_context(request, session, offset), nav="explorer"
    )


@router.post("/explorer/search")
async def explorer_search(
    request: Request, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        values = {"query": form.get("query") or "", "limit": form.get("limit") or "5"}
        try:
            payload = DiscoveryRequest.model_validate(values)
        except ValidationError as exc:
            return _explorer_response(
                request, session, status_code=422, form=values, errors=field_errors(exc)
            )
        try:
            results: DiscoveryResponse = run_discovery(
                session, request.app.state.search_provider, payload.query, payload.limit
            )
        except SearchProviderError as exc:
            return _explorer_response(
                request,
                session,
                status_code=exc.http_status,
                form=values,
                error=str(exc),
                error_hint=_PROVIDER_HINTS.get(exc.code),
            )
        except DiscoveryPersistenceError as exc:
            return _explorer_response(
                request, session, status_code=500, form=values, error=str(exc)
            )
        return _explorer_response(request, session, form=values, results=results)

    return await handle_post(request, SEARCH_FIELDS, handle)


# -- company detail ---------------------------------------------------------------
def _criteria_form(criteria: Any) -> dict[str, str]:
    return {
        "profile": criteria.profile,
        "target_keywords": ", ".join(criteria.target_keywords),
        "target_locations": ", ".join(criteria.target_locations),
        "target_tlds": ", ".join(criteria.target_tlds),
        "excluded_keywords": ", ".join(criteria.excluded_keywords),
        "threshold": str(criteria.threshold),
        "min_evidence_coverage": str(criteria.min_evidence_coverage),
    }


def _company_context(session: Session, candidate_id: int, **extra: Any) -> dict[str, Any]:
    company = get_enrichment(session, candidate_id)
    attempts = [to_summary(row) for row in list_attempts(session, candidate_id)]
    total_runs, rows = list_qualifications(session, candidate_id, limit=5)
    history = [to_qualification_record(row) for row in rows]
    latest = history[0] if history else None
    criteria = latest.criteria if latest is not None else QualificationCriteria()
    context: dict[str, Any] = {
        "company": company,
        "attempts": attempts,
        "latest": latest,
        "history": history,
        "total_runs": total_runs,
        "saved": find_saved_prospect(session, candidate_id),
        "criteria_form": _criteria_form(criteria),
        "criteria_errors": {},
        "error": None,
    }
    context.update(extra)
    return context


def _company_page(
    request: Request, session: Session, candidate_id: int, *, status_code: int = 200, **extra: Any
) -> Response:
    try:
        context = _company_context(session, candidate_id, **extra)
    except CandidateNotFoundError as exc:
        return render_error_page(request, 404, "candidate_not_found", str(exc))
    return render(
        request, "companies/detail.html", context, status_code=status_code, nav="explorer"
    )


@router.get("/companies/{candidate_id}")
def company_page(
    request: Request, candidate_id: CandidateId, session: Session = Depends(get_session)
) -> Response:
    return _company_page(request, session, candidate_id)


@router.post("/companies/{candidate_id}/research")
async def company_research(
    request: Request,
    candidate_id: CandidateId,
    session: Session = Depends(get_session),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> Response:
    def handle(form: FormData) -> Response:
        hours = settings_of(request).research_freshness_hours
        try:
            run = run_research(
                session_factory,
                request.app.state.website_fetcher,
                candidate_id,
                refresh=form.get("refresh") == "true",
                freshness=timedelta(hours=hours),
            )
        except CandidateNotFoundError as exc:
            return render_error_page(request, 404, "candidate_not_found", str(exc))
        except CandidateWebsiteMissingError as exc:
            return _company_page(request, session, candidate_id, status_code=409, error=str(exc))
        except ResearchPersistenceError as exc:
            return _company_page(request, session, candidate_id, status_code=500, error=str(exc))
        notice = research_notice(run.attempt.outcome, reused=run.reused)
        return redirect(f"/ui/companies/{candidate_id}", notice=notice)

    return await handle_post(request, ("refresh",), handle)


@router.post("/companies/{candidate_id}/qualify")
async def company_qualify(
    request: Request, candidate_id: CandidateId, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        values = {name: form.get(name) or "" for name in QUALIFY_FIELDS}
        payload: dict[str, Any] = {
            "profile": values["profile"],
            "target_keywords": split_terms(values["target_keywords"]),
            "target_locations": split_terms(values["target_locations"]),
            "target_tlds": split_terms(values["target_tlds"]),
            "excluded_keywords": split_terms(values["excluded_keywords"]),
            "threshold": values["threshold"],
            "min_evidence_coverage": values["min_evidence_coverage"],
        }
        try:
            criteria = QualificationCriteria.model_validate(payload)
        except ValidationError as exc:
            return _company_page(
                request,
                session,
                candidate_id,
                status_code=422,
                criteria_form=values,
                criteria_errors=field_errors(exc),
            )
        try:
            run_qualification(session, candidate_id, criteria)
        except CandidateNotFoundError as exc:
            return render_error_page(request, 404, "candidate_not_found", str(exc))
        except QualificationPersistenceError as exc:
            return _company_page(request, session, candidate_id, status_code=500, error=str(exc))
        return redirect(f"/ui/companies/{candidate_id}", notice="qualified")

    return await handle_post(request, QUALIFY_FIELDS, handle)


@router.post("/companies/{candidate_id}/save")
async def company_save(
    request: Request, candidate_id: CandidateId, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        try:
            result = save_prospect(session, candidate_id)
        except CandidateNotFoundError as exc:
            return render_error_page(request, 404, "candidate_not_found", str(exc))
        except (QualificationRequiredError, ProspectNotQualifiedError) as exc:
            return _company_page(request, session, candidate_id, status_code=409, error=str(exc))
        except QualificationNotFoundError as exc:
            return _company_page(request, session, candidate_id, status_code=404, error=str(exc))
        except ProspectPersistenceError as exc:
            return _company_page(request, session, candidate_id, status_code=500, error=str(exc))
        notice = "prospect_saved" if result.created else "prospect_exists"
        return redirect(f"/ui/prospects/{result.prospect.id}", notice=notice)

    return await handle_post(request, (), handle)
