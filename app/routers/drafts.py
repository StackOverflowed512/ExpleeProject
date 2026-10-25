"""Outreach draft endpoints: read, approve, send, and resolve delivery.

Sending is always for one existing draft and uses its recorded approval;
no endpoint accepts a recipient, subject, or body to send. The app has no
authentication: approval is an explicit local action recorded with the
configured ``APPROVAL_ACTOR`` label, not proof of a verified identity.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import get_session, get_session_factory
from app.errors import AppError
from app.routers.params import DraftId
from app.schemas.drafts import (
    ApproveDraftRequest,
    ApproveDraftResponse,
    DraftDetail,
    ResolveDeliveryRequest,
    SendAttemptRecord,
    SendDraftRequest,
    SendDraftResponse,
)
from app.services.approvals import (
    ApprovalPersistenceError,
    DraftAlreadyApprovedError,
    DraftNotApprovableError,
    DraftRevisionMismatchError,
    approve_draft,
)
from app.services.delivery import (
    ApprovalContentMismatchError,
    DeliveryDisabledError,
    DeliveryFailed,
    DeliveryInProgressError,
    DeliveryNotResolvableError,
    DeliveryOutcomeUncertainError,
    DeliveryPersistenceError,
    DraftNotApprovedError,
    ProspectAlreadyEmailedError,
    draft_detail,
    resolve_delivery,
    send_draft,
)
from app.services.drafts import DraftNotFoundError, get_draft
from app.services.providers.email import EmailDeliveryError, EmailProvider

router = APIRouter(tags=["drafts"])

# Domain exception -> (stable code, HTTP status).
DRAFT_ERRORS: dict[type[Exception], tuple[str, int]] = {
    DraftNotFoundError: ("draft_not_found", 404),
    DraftRevisionMismatchError: ("draft_revision_mismatch", 409),
    DraftAlreadyApprovedError: ("draft_already_approved", 409),
    DraftNotApprovableError: ("invalid_draft_state", 409),
    DraftNotApprovedError: ("draft_not_approved", 409),
    ApprovalContentMismatchError: ("approval_content_mismatch", 409),
    DeliveryDisabledError: ("delivery_disabled", 409),
    DeliveryInProgressError: ("delivery_in_progress", 409),
    DeliveryOutcomeUncertainError: ("delivery_outcome_uncertain", 409),
    ProspectAlreadyEmailedError: ("prospect_already_emailed", 409),
    DeliveryNotResolvableError: ("delivery_not_resolvable", 409),
    ApprovalPersistenceError: ("approval_persistence_failed", 500),
    DeliveryPersistenceError: ("delivery_persistence_failed", 500),
}
_STATE_CONFLICTS = {409: {"description": "Invalid state transition (error envelope)."}}
_NOT_FOUND = {404: {"description": "Unknown draft (error envelope)."}}
_VALIDATION = {422: {"description": "Request validation failed (error envelope)."}}


def _app_error(exc: Exception) -> AppError:
    if isinstance(exc, EmailDeliveryError):
        return AppError(str(exc), code=exc.code, status_code=exc.http_status)
    code, status_code = DRAFT_ERRORS[type(exc)]
    return AppError(str(exc), code=code, status_code=status_code)


def _detail(session: Session, draft_id: int) -> DraftDetail:
    session.expire_all()
    return draft_detail(session, get_draft(session, draft_id))


@router.get(
    "/drafts/{draft_id}",
    response_model=DraftDetail,
    summary="Get an outreach draft",
    description=(
        "Returns one draft with its audit trail: prompt version, model, "
        "provenance, approval (if any), and every send attempt, oldest first."
    ),
    responses={**_NOT_FOUND, **_VALIDATION},
)
def read_draft(
    draft_id: DraftId,
    session: Session = Depends(get_session),
) -> DraftDetail:
    try:
        return _detail(session, draft_id)
    except DraftNotFoundError as exc:
        raise _app_error(exc) from exc


@router.post(
    "/drafts/{draft_id}/approve",
    response_model=ApproveDraftResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Approve a draft for one recipient",
    description=(
        "Records an explicit human approval of this exact draft revision for the "
        "given recipient (never guessed). `revision` must match the draft. "
        "Returns 201 for a new approval, or 200 if the identical approval already "
        "exists. A different recipient is refused (409); generate a new revision "
        "instead. The app has no authentication: `approved_by` is the configured "
        "APPROVAL_ACTOR label, not a verified identity."
    ),
    responses={
        200: {"description": "Identical approval already recorded."},
        **_NOT_FOUND,
        **_STATE_CONFLICTS,
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def approve(
    draft_id: DraftId,
    payload: ApproveDraftRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> ApproveDraftResponse:
    settings: Settings = request.app.state.settings
    try:
        result = approve_draft(
            session,
            draft_id,
            recipient=payload.recipient_email,
            revision=payload.revision,
            actor=settings.approval_actor,
        )
    except tuple(DRAFT_ERRORS) as exc:
        raise _app_error(exc) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ApproveDraftResponse(created=result.created, draft=_detail(session, draft_id))


@router.post(
    "/drafts/{draft_id}/send",
    response_model=SendDraftResponse,
    summary="Send (or dry-run) one approved draft",
    description=(
        "Delivers exactly one approved draft using its recorded approval; no "
        "recipient or content can be supplied. `mode: dry_run` records an attempt "
        "without contacting any provider. `mode: deliver` needs EMAIL_DRY_RUN=false. "
        "A draft that was already sent is never sent again (`already_sent`). "
        "Provider failures return the error envelope with `details.attempt_id` and "
        "`details.retry_safe`; when false the outcome is uncertain and the draft "
        "must be resolved with POST /drafts/{id}/resolve-delivery."
    ),
    responses={
        **_NOT_FOUND,
        **_STATE_CONFLICTS,
        **_VALIDATION,
        500: {"description": "Persistence failure (error envelope)."},
        502: {"description": "Delivery failed or outcome uncertain (error envelope)."},
        503: {"description": "Email provider not configured (error envelope)."},
        504: {"description": "Mail server timeout before sending (error envelope)."},
    },
)
def send(
    draft_id: DraftId,
    payload: SendDraftRequest,
    request: Request,
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SendDraftResponse:
    settings: Settings = request.app.state.settings
    provider: EmailProvider = request.app.state.email_provider
    try:
        result = send_draft(
            session_factory,
            provider,
            draft_id,
            mode=payload.mode,
            dry_run_only=settings.email_dry_run,
            sender=settings.email_sender,
        )
    except DeliveryFailed as exc:
        raise AppError(
            str(exc.cause),
            code=exc.cause.code,
            status_code=exc.cause.http_status,
            details={"attempt_id": exc.attempt_id, "retry_safe": not exc.cause.uncertain},
        ) from exc
    except (*tuple(DRAFT_ERRORS), EmailDeliveryError) as exc:
        raise _app_error(exc) from exc
    with session_factory() as session:
        detail = _detail(session, draft_id)
    return SendDraftResponse(
        outcome=result.outcome,
        attempt=SendAttemptRecord.model_validate(result.attempt, from_attributes=True),
        draft=detail,
    )


@router.post(
    "/drafts/{draft_id}/resolve-delivery",
    response_model=DraftDetail,
    summary="Record the verified outcome of an uncertain delivery",
    description=(
        "Manual recovery after an uncertain or interrupted send. First check the "
        "mailbox or mail server logs for the attempt's Message-ID. "
        "`delivered: true` marks the draft sent; `false` returns it to approved "
        "so it can be sent again. Allowed only for 'delivery_uncertain' drafts, or "
        "'sending' drafts whose claim is older than 15 minutes."
    ),
    responses={**_NOT_FOUND, **_STATE_CONFLICTS, **_VALIDATION},
)
def resolve(
    draft_id: DraftId,
    payload: ResolveDeliveryRequest,
    session: Session = Depends(get_session),
) -> DraftDetail:
    try:
        resolve_delivery(session, draft_id, delivered=payload.delivered, note=payload.note)
    except tuple(DRAFT_ERRORS) as exc:
        raise _app_error(exc) from exc
    return _detail(session, draft_id)
