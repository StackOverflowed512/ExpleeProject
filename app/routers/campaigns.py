"""Outreach campaign and draft-generation endpoints.

No endpoint here sends email or changes a draft's state: drafts are created
as ``pending`` and stay that way until Phase 5 adds approval and delivery.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, status
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session, get_session_factory
from app.errors import AppError
from app.routers.params import CampaignId
from app.schemas.campaigns import CampaignCreate, CampaignDetail, CampaignListResponse
from app.schemas.drafts import (
    DraftGenerationItem,
    DraftListResponse,
    GenerateDraftsRequest,
    GenerateDraftsResponse,
)
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
    generate_drafts,
    list_drafts,
    to_draft_record,
)
from app.services.providers.drafts import DraftGenerator

router = APIRouter(tags=["campaigns"])

_CAMPAIGN_NOT_FOUND = {404: {"description": "Unknown campaign (error envelope)."}}
_VALIDATION = {422: {"description": "Request validation failed (error envelope)."}}


def _campaign_not_found(exc: CampaignNotFoundError) -> AppError:
    return AppError(str(exc), code="campaign_not_found", status_code=404)


@router.post(
    "/campaigns",
    response_model=CampaignDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a campaign from saved prospects",
    description=(
        "Creates a campaign with its configuration and the given saved prospect "
        "IDs (order kept). Every ID must be an existing saved prospect; "
        "duplicates and unknown fields are rejected."
    ),
    responses={
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def create(
    payload: CampaignCreate,
    session: Session = Depends(get_session),
) -> CampaignDetail:
    try:
        campaign = create_campaign(session, payload)
    except InvalidProspectIdsError as exc:
        raise AppError(
            str(exc),
            code="invalid_prospect_ids",
            status_code=422,
            details={"missing_prospect_ids": exc.missing},
        ) from exc
    except CampaignPersistenceError as exc:
        raise AppError(str(exc), code="campaign_persistence_failed", status_code=500) from exc
    return to_campaign_detail(campaign, campaign_prospect_rows(session, campaign.id))


@router.get(
    "/campaigns",
    response_model=CampaignListResponse,
    summary="List campaigns",
    description="Campaigns newest first (ties by ID) with bounded pagination.",
    responses=_VALIDATION,
)
def read_campaigns(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    session: Session = Depends(get_session),
) -> CampaignListResponse:
    total, rows = list_campaigns(session, limit=limit, offset=offset)
    return CampaignListResponse(
        items=[to_campaign_summary(campaign, count) for campaign, count in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/campaigns/{campaign_id}",
    response_model=CampaignDetail,
    summary="Get a campaign and its prospects",
    responses={**_CAMPAIGN_NOT_FOUND, **_VALIDATION},
)
def read_campaign(
    campaign_id: CampaignId,
    session: Session = Depends(get_session),
) -> CampaignDetail:
    try:
        campaign = get_campaign(session, campaign_id)
    except CampaignNotFoundError as exc:
        raise _campaign_not_found(exc) from exc
    return to_campaign_detail(campaign, campaign_prospect_rows(session, campaign_id))


@router.post(
    "/campaigns/{campaign_id}/drafts",
    response_model=GenerateDraftsResponse,
    summary="Generate outreach drafts for campaign prospects",
    description=(
        "Generates one `pending` draft per campaign prospect with the configured "
        "draft generator, in campaign order, at most "
        f"{BATCH_LIMIT} per request (`remaining` reports the rest). Prospects "
        "that already have a draft are skipped and returned as `existing` unless "
        "`regenerate` is true, which adds a new revision and keeps earlier drafts. "
        "Each draft is validated before it is saved; if the provider fails, the "
        "request stops with an error envelope whose details list drafts already "
        "saved, and nothing partial is stored. No email is sent."
    ),
    responses={
        **_CAMPAIGN_NOT_FOUND,
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
        502: {"description": "Provider failure or invalid output (error envelope)."},
        503: {"description": "Provider not configured or rate limited (error envelope)."},
        504: {"description": "Provider timeout (error envelope)."},
    },
)
def create_drafts(
    campaign_id: CampaignId,
    request: Request,
    payload: Annotated[GenerateDraftsRequest | None, Body()] = None,
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> GenerateDraftsResponse:
    generator: DraftGenerator = request.app.state.draft_generator
    options = payload or GenerateDraftsRequest()
    try:
        # The service opens its own short sessions; none is held during the
        # provider call.
        result = generate_drafts(
            session_factory,
            generator,
            campaign_id,
            saved_prospect_ids=options.saved_prospect_ids,
            regenerate=options.regenerate,
        )
    except CampaignNotFoundError as exc:
        raise _campaign_not_found(exc) from exc
    except ProspectNotInCampaignError as exc:
        raise AppError(
            str(exc),
            code="prospect_not_in_campaign",
            status_code=422,
            details={"missing_saved_prospect_ids": exc.missing},
        ) from exc
    except DraftGenerationFailed as exc:
        raise AppError(
            str(exc.cause),
            code=exc.cause.code,
            status_code=exc.cause.http_status,
            details={
                "failed_saved_prospect_id": exc.failed_saved_prospect_id,
                "generated_draft_ids": exc.generated_draft_ids,
            },
        ) from exc
    except DraftPersistenceError as exc:
        raise AppError(str(exc), code="draft_persistence_failed", status_code=500) from exc

    return GenerateDraftsResponse(
        campaign_id=result.campaign_id,
        batch_limit=BATCH_LIMIT,
        results=[
            DraftGenerationItem(
                saved_prospect_id=item.saved_prospect_id,
                outcome=item.outcome,
                draft=to_draft_record(item.draft),
            )
            for item in result.items
        ],
        remaining=result.remaining,
    )


@router.get(
    "/campaigns/{campaign_id}/drafts",
    response_model=DraftListResponse,
    summary="List a campaign's drafts",
    description=(
        "Drafts in campaign order, then revision (ties by ID), with bounded "
        "pagination. Optionally filter by saved prospect."
    ),
    responses={**_CAMPAIGN_NOT_FOUND, **_VALIDATION},
)
def read_drafts(
    campaign_id: CampaignId,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    saved_prospect_id: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    session: Session = Depends(get_session),
) -> DraftListResponse:
    try:
        total, rows = list_drafts(
            session,
            campaign_id,
            limit=limit,
            offset=offset,
            saved_prospect_id=saved_prospect_id,
        )
    except CampaignNotFoundError as exc:
        raise _campaign_not_found(exc) from exc
    return DraftListResponse(
        items=[to_draft_record(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
