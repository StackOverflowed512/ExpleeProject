"""Outreach campaigns: list, create, detail, and explicit draft generation."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session, get_session_factory
from app.routers.params import CampaignId
from app.routers.ui.common import PAGE_SIZE, Offset, handle_post, page_links, settings_of
from app.schemas.campaigns import MAX_CAMPAIGN_PROSPECTS, CampaignCreate
from app.schemas.prospects import ProspectQuery
from app.services.campaigns import (
    CampaignNotFoundError,
    CampaignPersistenceError,
    InvalidProspectIdsError,
    campaign_prospect_rows,
    create_campaign,
    get_campaign,
    list_campaigns,
    to_campaign_detail,
    to_campaign_summary,
)
from app.services.drafts import (
    BATCH_LIMIT,
    DraftGenerationFailed,
    DraftPersistenceError,
    ProspectNotInCampaignError,
    campaign_draft_summary,
    generate_drafts,
)
from app.services.prospects import list_prospects, to_prospect_summary
from app.web.forms import FormData, field_errors
from app.web.rendering import redirect, render, render_error_page

router = APIRouter()

CAMPAIGN_FIELDS = (
    "name",
    "description",
    "product",
    "value_proposition",
    "target_audience",
    "call_to_action",
    "tone",
    "sender_name",
    "sender_company",
)
TONES = ("professional", "friendly", "concise", "formal")
SELECTABLE_LIMIT = 100


@router.get("/campaigns")
def campaigns_page(
    request: Request, offset: Offset = 0, session: Session = Depends(get_session)
) -> Response:
    total, rows = list_campaigns(session, limit=PAGE_SIZE, offset=offset)
    return render(
        request,
        "campaigns/index.html",
        {
            "campaigns": [to_campaign_summary(c, count) for c, count in rows],
            "pages": page_links(total, offset),
        },
        nav="campaigns",
    )


def _new_campaign_page(
    request: Request,
    session: Session,
    *,
    values: dict[str, str] | None = None,
    selected: list[int] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    total, rows = list_prospects(
        session, ProspectQuery(limit=SELECTABLE_LIMIT, sort="name_asc")
    )
    return render(
        request,
        "campaigns/new.html",
        {
            "values": values or {name: "" for name in CAMPAIGN_FIELDS} | {"tone": "professional"},
            "selected": selected or [],
            "errors": errors or {},
            "prospects": [to_prospect_summary(*row) for row in rows],
            "prospect_total": total,
            "selectable_limit": SELECTABLE_LIMIT,
            "max_prospects": MAX_CAMPAIGN_PROSPECTS,
            "tones": TONES,
        },
        status_code=status_code,
        nav="campaigns",
    )


@router.get("/campaigns/new")
def new_campaign_page(request: Request, session: Session = Depends(get_session)) -> Response:
    return _new_campaign_page(request, session)


@router.post("/campaigns")
async def create_campaign_action(
    request: Request, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        values = {name: form.get(name) or "" for name in CAMPAIGN_FIELDS}
        raw_ids = form.getlist("prospect_ids")
        selected: list[int] = []
        for raw in raw_ids:
            if not raw.isdigit():
                return _new_campaign_page(
                    request,
                    session,
                    values=values,
                    errors={"prospect_ids": "Select prospects from the list."},
                    status_code=422,
                )
            selected.append(int(raw))
        try:
            payload = CampaignCreate.model_validate(values | {"prospect_ids": selected})
        except ValidationError as exc:
            return _new_campaign_page(
                request,
                session,
                values=values,
                selected=selected,
                errors=field_errors(exc),
                status_code=422,
            )
        try:
            campaign = create_campaign(session, payload)
        except InvalidProspectIdsError as exc:
            return _new_campaign_page(
                request,
                session,
                values=values,
                selected=selected,
                errors={"prospect_ids": str(exc)},
                status_code=422,
            )
        except CampaignPersistenceError as exc:
            return render_error_page(request, 500, "campaign_persistence_failed", str(exc))
        return redirect(f"/ui/campaigns/{campaign.id}", notice="campaign_created")

    return await handle_post(request, (*CAMPAIGN_FIELDS, "prospect_ids"), handle)


def _campaign_page(
    request: Request,
    session: Session,
    campaign_id: int,
    *,
    status_code: int = 200,
    error: str | None = None,
    error_details: list[str] | None = None,
) -> Response:
    try:
        campaign = get_campaign(session, campaign_id)
    except CampaignNotFoundError as exc:
        return render_error_page(request, 404, "campaign_not_found", str(exc))
    detail = to_campaign_detail(campaign, campaign_prospect_rows(session, campaign_id))
    summary = campaign_draft_summary(session, campaign_id)
    rows = []
    for item in detail.prospects:
        drafts = summary.get(item.prospect.id)
        rows.append(
            {
                "item": item,
                "latest": drafts.latest if drafts else None,
                "revisions": drafts.revisions if drafts else 0,
            }
        )
    total = sum(drafts.revisions for drafts in summary.values())
    settings = settings_of(request)
    return render(
        request,
        "campaigns/detail.html",
        {
            "campaign": detail,
            "rows": rows,
            "draft_total": total,
            "without_draft": sum(1 for row in rows if row["latest"] is None),
            "batch_limit": BATCH_LIMIT,
            "generator": settings.draft_generator,
            "model": settings.mistral_model if settings.draft_generator == "mistral" else None,
            "error": error,
            "error_details": error_details or [],
        },
        status_code=status_code,
        nav="campaigns",
    )


@router.get("/campaigns/{campaign_id}")
def campaign_page(
    request: Request, campaign_id: CampaignId, session: Session = Depends(get_session)
) -> Response:
    return _campaign_page(request, session, campaign_id)


@router.post("/campaigns/{campaign_id}/drafts")
async def generate_campaign_drafts(
    request: Request,
    campaign_id: CampaignId,
    session: Session = Depends(get_session),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> Response:
    def handle(form: FormData) -> Response:
        try:
            result = generate_drafts(
                session_factory,
                request.app.state.draft_generator,
                campaign_id,
                regenerate=form.get("regenerate") == "true",
            )
        except CampaignNotFoundError as exc:
            return render_error_page(request, 404, "campaign_not_found", str(exc))
        except ProspectNotInCampaignError as exc:
            return _campaign_page(request, session, campaign_id, status_code=422, error=str(exc))
        except DraftGenerationFailed as exc:
            details = [f"Error code: {exc.cause.code}."]
            if exc.generated_draft_ids:
                details.append(
                    f"{len(exc.generated_draft_ids)} draft(s) generated before the failure "
                    "were saved."
                )
            return _campaign_page(
                request,
                session,
                campaign_id,
                status_code=exc.cause.http_status,
                error=f"Draft generation stopped: {exc.cause}",
                error_details=details,
            )
        except DraftPersistenceError as exc:
            return _campaign_page(request, session, campaign_id, status_code=500, error=str(exc))
        notice = "drafts_remaining" if result.remaining else "drafts_generated"
        return redirect(f"/ui/campaigns/{campaign_id}", notice=notice)

    return await handle_post(request, ("regenerate",), handle)
