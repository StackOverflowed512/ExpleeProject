"""Draft review, explicit approval, single-draft sending, and recovery.

Safety rules for this module:

* Approving and sending are separate POST requests. Neither happens on a
  page load, and the UI never retries.
* The approve form accepts only the recipient, the revision being reviewed,
  and a confirmation; the send form accepts only the mode and a confirmation.
  Subject and body are never accepted from the browser; any extra field is
  refused before the service is called.
* Every rule (state, approval match, configuration, concurrency,
  idempotency) is enforced by ``app.services.approvals`` and
  ``app.services.delivery``. The buttons shown here are only hints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session, get_session_factory
from app.routers.drafts import DRAFT_ERRORS
from app.routers.params import DraftId
from app.routers.ui.common import PAGE_SIZE, Offset, page_links, handle_post, settings_of
from app.schemas.drafts import ApproveDraftRequest, ResolveDeliveryRequest, SendDraftRequest
from app.services.approvals import approve_draft
from app.services.campaigns import CampaignNotFoundError, get_campaign
from app.services.delivery import (
    STALE_SENDING_AFTER,
    DeliveryFailed,
    draft_detail,
    resolve_delivery,
    send_draft,
)
from app.services.drafts import (
    DraftNotFoundError,
    get_draft,
    list_drafts,
    list_drafts_overview,
    to_draft_record,
)
from app.services.prospects import ProspectNotFoundError, get_prospect, to_prospect_summary
from app.services.providers.email import EmailDeliveryError
from app.web import presentation
from app.web.forms import FormData, field_errors
from app.web.rendering import redirect, render, render_error_page, wants_fragment

router = APIRouter()

APPROVE_FIELDS = ("recipient_email", "revision", "confirm_reviewed")
SEND_FIELDS = ("mode", "confirm_send")
RESOLVE_FIELDS = ("delivered", "note", "confirm_checked")

VIEWS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "review": ("Awaiting approval", ("pending",)),
    "approved": ("Approved, not sent", ("approved",)),
    "attention": ("Needs attention", ("delivery_uncertain", "sending")),
    "sent": ("Sent", ("sent",)),
    "all": ("All drafts", None),
}


@router.get("/drafts")
def drafts_page(
    request: Request,
    view: str = "review",
    offset: Offset = 0,
    session: Session = Depends(get_session),
) -> Response:
    if view not in VIEWS:
        view = "review"
    total, items = list_drafts_overview(
        session, states=VIEWS[view][1], limit=PAGE_SIZE, offset=offset
    )
    return render(
        request,
        "drafts/index.html",
        {
            "views": VIEWS,
            "view": view,
            "items": items,
            "pages": page_links(total, offset),
        },
        nav="drafts",
    )


def _review_context(request: Request, session: Session, draft_id: int) -> dict[str, Any]:
    session.expire_all()
    draft = get_draft(session, draft_id)
    detail = draft_detail(session, draft)
    settings = settings_of(request)
    try:
        campaign = get_campaign(session, detail.campaign_id)
    except CampaignNotFoundError:  # pragma: no cover - guarded by foreign keys
        campaign = None
    try:
        prospect = to_prospect_summary(*get_prospect(session, detail.saved_prospect_id))
    except ProspectNotFoundError:  # pragma: no cover - guarded by foreign keys
        prospect = None
    _, revisions = list_drafts(
        session,
        detail.campaign_id,
        limit=100,
        offset=0,
        saved_prospect_id=detail.saved_prospect_id,
    )
    return {
        "draft": detail,
        "campaign": campaign,
        "prospect": prospect,
        "revisions": [to_draft_record(row) for row in revisions],
        "actions": presentation.draft_actions(detail),
        "mode": presentation.delivery_mode(settings, request.app.state.email_provider),
        "sender": settings.email_sender,
        "stale_minutes": int(STALE_SENDING_AFTER.total_seconds() // 60),
        "approve_form": {"recipient_email": ""},
        "approve_errors": {},
        "send_errors": {},
        "resolve_form": {"delivered": "", "note": ""},
        "resolve_errors": {},
        "error": None,
    }


def _review_page(
    request: Request,
    session: Session,
    draft_id: int,
    *,
    status_code: int = 200,
    **extra: Any,
) -> Response:
    try:
        context = _review_context(request, session, draft_id)
    except DraftNotFoundError as exc:
        return render_error_page(request, 404, "draft_not_found", str(exc))
    context.update(extra)
    return render(request, "drafts/detail.html", context, status_code=status_code, nav="drafts")


@router.get("/drafts/{draft_id}")
def draft_page(
    request: Request, draft_id: DraftId, session: Session = Depends(get_session)
) -> Response:
    return _review_page(request, session, draft_id)


@router.get("/drafts/{draft_id}/delivery")
def draft_delivery_panel(
    request: Request, draft_id: DraftId, session: Session = Depends(get_session)
) -> Response:
    """Read-only delivery panel, polled by HTMX while a send is in progress."""
    if not wants_fragment(request, "delivery-panel"):
        return redirect(f"/ui/drafts/{draft_id}")
    try:
        context = _review_context(request, session, draft_id)
    except DraftNotFoundError as exc:
        return render_error_page(request, 404, "draft_not_found", str(exc))
    return render(request, "drafts/_delivery_panel.html", context)


def _domain_error(exc: Exception) -> tuple[str, int]:
    if isinstance(exc, EmailDeliveryError):
        return exc.code, exc.http_status
    return DRAFT_ERRORS[type(exc)]


@router.post("/drafts/{draft_id}/approve")
async def approve_action(
    request: Request, draft_id: DraftId, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        recipient = (form.get("recipient_email") or "").strip()
        values = {"recipient_email": recipient}
        if form.get("confirm_reviewed") != "yes":
            return _review_page(
                request,
                session,
                draft_id,
                status_code=422,
                approve_form=values,
                approve_errors={
                    "confirm_reviewed": "Confirm that you reviewed the recipient, subject, "
                    "and full message."
                },
            )
        try:
            payload = ApproveDraftRequest.model_validate(
                {"recipient_email": recipient, "revision": form.get("revision") or ""}
            )
        except ValidationError as exc:
            return _review_page(
                request,
                session,
                draft_id,
                status_code=422,
                approve_form=values,
                approve_errors=field_errors(exc),
            )
        try:
            result = approve_draft(
                session,
                draft_id,
                recipient=payload.recipient_email,
                revision=payload.revision,
                actor=settings_of(request).approval_actor,
            )
        except tuple(DRAFT_ERRORS) as exc:
            code, status_code = _domain_error(exc)
            if code == "draft_not_found":
                return render_error_page(request, 404, code, str(exc))
            return _review_page(
                request,
                session,
                draft_id,
                status_code=status_code,
                approve_form=values,
                error=str(exc),
            )
        notice = "approved" if result.created else "approval_exists"
        return redirect(f"/ui/drafts/{draft_id}", notice=notice)

    return await handle_post(request, APPROVE_FIELDS, handle)


@router.post("/drafts/{draft_id}/send")
async def send_action(
    request: Request,
    draft_id: DraftId,
    session: Session = Depends(get_session),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> Response:
    def handle(form: FormData) -> Response:
        try:
            payload = SendDraftRequest.model_validate({"mode": form.get("mode") or ""})
        except ValidationError as exc:
            return _review_page(
                request, session, draft_id, status_code=422, send_errors=field_errors(exc)
            )
        if payload.mode == "deliver" and form.get("confirm_send") != "yes":
            return _review_page(
                request,
                session,
                draft_id,
                status_code=422,
                send_errors={
                    "confirm_send": "Confirm that you want to send this one email now."
                },
            )
        settings = settings_of(request)
        try:
            result = send_draft(
                session_factory,
                request.app.state.email_provider,
                draft_id,
                mode=payload.mode,
                dry_run_only=settings.email_dry_run,
                sender=settings.email_sender,
            )
        except DeliveryFailed as exc:
            if exc.cause.uncertain:
                return redirect(f"/ui/drafts/{draft_id}", notice="delivery_uncertain")
            message = (
                f"Delivery failed: {presentation.failure_text(exc.cause.code)} "
                "The draft is still approved, so you may try again."
            )
            return _review_page(
                request, session, draft_id, status_code=exc.cause.http_status, error=message
            )
        except (*tuple(DRAFT_ERRORS), EmailDeliveryError) as exc:
            code, status_code = _domain_error(exc)
            if code == "draft_not_found":
                return render_error_page(request, 404, code, str(exc))
            return _review_page(request, session, draft_id, status_code=status_code, error=str(exc))
        return redirect(f"/ui/drafts/{draft_id}", notice=result.outcome)

    return await handle_post(request, SEND_FIELDS, handle)


@router.post("/drafts/{draft_id}/resolve")
async def resolve_action(
    request: Request, draft_id: DraftId, session: Session = Depends(get_session)
) -> Response:
    def handle(form: FormData) -> Response:
        values = {"delivered": form.get("delivered") or "", "note": form.get("note") or ""}
        errors: dict[str, str] = {}
        if values["delivered"] not in ("yes", "no"):
            errors["delivered"] = "Choose whether the message was delivered."
        if form.get("confirm_checked") != "yes":
            errors["confirm_checked"] = (
                "Confirm that you checked the mailbox or mail server logs."
            )
        payload = None
        if not errors:
            try:
                payload = ResolveDeliveryRequest.model_validate(
                    {
                        "delivered": values["delivered"] == "yes",
                        "note": values["note"].strip() or None,
                    }
                )
            except ValidationError as exc:
                errors = field_errors(exc)
        if payload is None:
            return _review_page(
                request,
                session,
                draft_id,
                status_code=422,
                resolve_form=values,
                resolve_errors=errors,
            )
        try:
            resolve_delivery(session, draft_id, delivered=payload.delivered, note=payload.note)
        except tuple(DRAFT_ERRORS) as exc:
            code, status_code = _domain_error(exc)
            if code == "draft_not_found":
                return render_error_page(request, 404, code, str(exc))
            return _review_page(
                request,
                session,
                draft_id,
                status_code=status_code,
                resolve_form=values,
                error=str(exc),
            )
        notice = "resolved_delivered" if payload.delivered else "resolved_not_delivered"
        return redirect(f"/ui/drafts/{draft_id}", notice=notice)

    return await handle_post(request, RESOLVE_FIELDS, handle)
