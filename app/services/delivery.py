"""Delivery of approved outreach drafts, with a full audit trail.

State machine (``outreach_drafts.state``)::

    pending --approve--> approved --send(deliver): claim--> sending
    sending --accepted--------------------> sent
    sending --definite failure------------> approved   (safe to retry)
    sending --uncertain outcome-----------> delivery_uncertain
    delivery_uncertain | stale sending --resolve(delivered)--> sent
    delivery_uncertain | stale sending --resolve(not)--------> approved

Dry runs never change the state and never call a provider.

``send_draft`` re-verifies everything itself before any delivery: the draft
exists; a recorded approval matches its exact revision, recipient, subject,
and body (content hash); it has not been sent; no in-flight or uncertain
delivery is unresolved; the configuration allows the mode; and no other
revision for the same campaign prospect is sending, sent, or possibly sent.

A real send runs in three steps so no database session is held during the
network call:

1. Claim: lock the draft row, verify, set ``sending``, and insert an
   ``in_progress`` attempt with a unique Message-ID, in one transaction.
2. Deliver through the injected :class:`EmailProvider`.
3. Finalize the attempt exactly once (``UPDATE ... WHERE outcome =
   'in_progress'``) and set ``sent``, ``approved``, or ``delivery_uncertain``.

Idempotency: a ``sent`` draft is never delivered again (``already_sent``).
Exactly-once delivery is **not** claimed: SMTP cannot guarantee it. When the
outcome is uncertain, or the process dies after claiming, the draft stays
``delivery_uncertain`` or ``sending`` and is never retried automatically.
An operator checks the mailbox or server logs and records the result with
:func:`resolve_delivery`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models.draft_approval import DraftApproval
from app.models.email_send_attempt import EmailSendAttempt
from app.models.outreach_draft import OutreachDraft
from app.schemas.drafts import (
    ApprovalRecord,
    DraftDetail,
    DraftRecord,
    SendAttemptRecord,
    SendOutcome,
)
from app.services.approvals import approval_matches, get_approval
from app.services.drafts import DraftNotFoundError
from app.services.providers.email import (
    EmailDeliveryError,
    EmailDeliveryUncertainError,
    EmailNotConfiguredError,
    EmailProvider,
    OutgoingEmail,
)

logger = logging.getLogger(__name__)

# A 'sending' claim older than this is treated as interrupted and may be
# resolved manually; younger claims may still be in flight.
STALE_SENDING_AFTER = timedelta(minutes=15)
DELIVERY_STATES = ("sending", "sent", "delivery_uncertain")


class DraftNotApprovedError(Exception):
    """The draft has no valid recorded approval."""


class ApprovalContentMismatchError(Exception):
    """The draft's content no longer matches its approval."""


class DeliveryDisabledError(Exception):
    """Real delivery was requested but the configuration only allows dry runs."""


class DeliveryInProgressError(Exception):
    """Another delivery attempt for this draft is in flight."""


class DeliveryOutcomeUncertainError(Exception):
    """A previous attempt may have delivered the message; resolve it first."""


class ProspectAlreadyEmailedError(Exception):
    """Another revision for the same campaign prospect was sent or is in flight."""


class DeliveryNotResolvableError(Exception):
    """The draft has no uncertain or interrupted delivery to resolve."""


class DeliveryPersistenceError(Exception):
    """A delivery attempt could not be recorded."""


class DeliveryFailed(Exception):
    """The provider reported a failure; the attempt was recorded.

    ``cause.uncertain`` tells a definite failure (safe to retry) from an
    uncertain one (the draft is now ``delivery_uncertain``).
    """

    def __init__(self, cause: EmailDeliveryError, *, attempt_id: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.attempt_id = attempt_id


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    attempt: EmailSendAttempt


@dataclass(frozen=True)
class _Claim:
    attempt_id: int
    message: OutgoingEmail


def message_id_for(draft_id: int, attempt_id: int, sender: str) -> str:
    """Unique Message-ID per attempt (traceable in the recipient's mailbox)."""
    domain = sender.rsplit("@", 1)[-1]
    return f"<explee.draft-{draft_id}.attempt-{attempt_id}@{domain}>"


def _lock_draft(session: Session, draft_id: int) -> OutreachDraft:
    draft = session.execute(
        select(OutreachDraft).where(OutreachDraft.id == draft_id).with_for_update()
    ).scalar_one_or_none()
    if draft is None:
        raise DraftNotFoundError(f"Draft {draft_id} was not found.")
    return draft


def _latest_attempt(
    session: Session, draft_id: int, *, outcome: str | None = None
) -> EmailSendAttempt | None:
    stmt = select(EmailSendAttempt).where(EmailSendAttempt.draft_id == draft_id)
    if outcome is not None:
        stmt = stmt.where(EmailSendAttempt.outcome == outcome)
    stmt = stmt.order_by(EmailSendAttempt.started_at.desc(), EmailSendAttempt.id.desc())
    return session.execute(stmt.limit(1)).scalar_one_or_none()


def _verified_approval(session: Session, draft: OutreachDraft) -> DraftApproval:
    if draft.state == "pending":
        raise DraftNotApprovedError(f"Draft {draft.id} has not been approved.")
    approval = get_approval(session, draft.id)
    if approval is None:
        raise DraftNotApprovedError(f"Draft {draft.id} has no recorded approval.")
    if not approval_matches(approval, draft):
        raise ApprovalContentMismatchError(
            f"Draft {draft.id} no longer matches its approval; it will not be sent."
        )
    return approval


def _other_delivery_exists(session: Session, draft: OutreachDraft) -> bool:
    other = session.execute(
        select(OutreachDraft.id).where(
            OutreachDraft.campaign_id == draft.campaign_id,
            OutreachDraft.saved_prospect_id == draft.saved_prospect_id,
            OutreachDraft.id != draft.id,
            OutreachDraft.state.in_(DELIVERY_STATES),
        )
    ).first()
    return other is not None


def _refuse_by_state(session: Session, draft: OutreachDraft) -> SendResult | None:
    """Handle states where no new attempt may start; ``None`` means continue."""
    if draft.state == "sent":
        attempt = _latest_attempt(session, draft.id, outcome="sent")
        if attempt is None:  # pragma: no cover - guarded by the state machine
            raise DeliveryPersistenceError(f"Draft {draft.id} is sent but has no record.")
        return SendResult(outcome="already_sent", attempt=attempt)
    if draft.state == "sending":
        raise DeliveryInProgressError(
            f"Draft {draft.id} is being delivered; wait for the result."
        )
    if draft.state == "delivery_uncertain":
        raise DeliveryOutcomeUncertainError(
            f"Draft {draft.id} may already have been delivered; resolve the "
            "uncertain attempt before sending again."
        )
    return None


def _verify_and_claim(
    session: Session,
    provider: EmailProvider,
    draft_id: int,
    *,
    mode: str,
    dry_run_only: bool,
    sender: str | None,
    started_at: datetime,
) -> SendResult | _Claim:
    """Inside the caller's transaction: refuse, record a dry run, or claim."""
    draft = _lock_draft(session, draft_id)
    done = _refuse_by_state(session, draft)
    if done is not None:
        return done
    approval = _verified_approval(session, draft)

    if mode == "dry_run":
        attempt = EmailSendAttempt(
            draft_id=draft.id,
            approval_id=approval.id,
            mode="dry_run",
            provider="none",
            outcome="dry_run",
            started_at=started_at,
            finished_at=started_at,
            duration_ms=0,
        )
        session.add(attempt)
        session.flush()
        logger.info("Dry run recorded for draft %s (attempt %s)", draft.id, attempt.id)
        return SendResult(outcome="dry_run", attempt=attempt)

    if dry_run_only:
        raise DeliveryDisabledError(
            "Real delivery is disabled (EMAIL_DRY_RUN=true); only dry runs are allowed."
        )
    if not sender:
        raise EmailNotConfiguredError("Real delivery needs EMAIL_SENDER to be configured.")
    if _other_delivery_exists(session, draft):
        raise ProspectAlreadyEmailedError(
            "Another draft for this campaign prospect was already sent or is in flight; "
            f"draft {draft.id} will not be sent."
        )

    attempt = EmailSendAttempt(
        draft_id=draft.id,
        approval_id=approval.id,
        mode="real",
        provider=provider.name[:50],
        outcome="in_progress",
        started_at=started_at,
    )
    session.add(attempt)
    session.flush()
    attempt.message_id = message_id_for(draft.id, attempt.id, sender)
    draft.state = "sending"
    return _Claim(
        attempt_id=attempt.id,
        message=OutgoingEmail(
            sender=sender,
            recipient=approval.recipient_email,
            subject=draft.subject,
            body=draft.body,
            message_id=attempt.message_id,
        ),
    )


def send_draft(
    session_factory: sessionmaker[Session],
    provider: EmailProvider,
    draft_id: int,
    *,
    mode: str,
    dry_run_only: bool,
    sender: str | None,
    now: datetime | None = None,
) -> SendResult:
    """Dry-run or deliver one approved draft (see the module docstring)."""
    started_at = now or datetime.now(UTC)

    # 1. Verify and claim (or record a dry run) in one short transaction.
    with session_factory() as session:
        try:
            with session.begin():
                step = _verify_and_claim(
                    session,
                    provider,
                    draft_id,
                    mode=mode,
                    dry_run_only=dry_run_only,
                    sender=sender,
                    started_at=started_at,
                )
            if isinstance(step, SendResult):
                # Load the row fully before the session closes.
                session.refresh(step.attempt)
                return step
            claim = step
        except IntegrityError as exc:
            # The partial unique indexes caught a concurrent claim.
            raise ProspectAlreadyEmailedError(
                f"Draft {draft_id} or another revision for the same prospect is already "
                "being delivered."
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("Delivery claim failed for draft %s", draft_id)
            raise DeliveryPersistenceError("The delivery attempt could not be recorded.") from exc

    # 2. Deliver with no database session open.
    clock = time.monotonic()
    error: EmailDeliveryError | None = None
    reference: str | None = None
    try:
        result = provider.send(claim.message)
        reference = result.provider_reference
    except EmailDeliveryError as exc:
        error = exc
    except Exception:  # noqa: BLE001 - unknown failures may have delivered the message
        logger.exception("Unexpected provider error for draft %s", draft_id)
        error = EmailDeliveryUncertainError(
            "An unexpected error occurred during delivery; it may have been delivered."
        )
    duration_ms = int((time.monotonic() - clock) * 1000)

    # 3. Finalize the attempt exactly once.
    if error is None:
        outcome, state, category = "sent", "sent", None
    elif error.uncertain:
        outcome, state, category = "uncertain", "delivery_uncertain", error.code
    else:
        outcome, state, category = "failed", "approved", error.code
    finished_at = started_at + timedelta(milliseconds=duration_ms) if now else datetime.now(UTC)
    with session_factory() as session:
        try:
            with session.begin():
                finalized = session.execute(
                    update(EmailSendAttempt)
                    .where(
                        EmailSendAttempt.id == claim.attempt_id,
                        EmailSendAttempt.outcome == "in_progress",
                    )
                    .values(
                        outcome=outcome,
                        failure_category=category,
                        provider_reference=reference,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                    )
                ).rowcount
                session.execute(
                    update(OutreachDraft)
                    .where(OutreachDraft.id == draft_id, OutreachDraft.state == "sending")
                    .values(state=state)
                )
                if finalized != 1:  # pragma: no cover - only if resolved concurrently
                    raise DeliveryPersistenceError("The attempt was already finalized.")
            attempt = session.get(EmailSendAttempt, claim.attempt_id, populate_existing=True)
        except SQLAlchemyError as exc:
            # The provider may have delivered; the draft stays 'sending' and
            # must be resolved manually. Never retried automatically.
            logger.critical(
                "Could not record the outcome of attempt %s for draft %s (provider "
                "outcome: %s); the draft stays 'sending' and needs manual resolution.",
                claim.attempt_id,
                draft_id,
                outcome,
            )
            raise DeliveryPersistenceError(
                "The delivery outcome could not be recorded; resolve the draft manually."
            ) from exc

    logger.info("Draft %s attempt %s finished: %s", draft_id, claim.attempt_id, outcome)
    if error is not None:
        raise DeliveryFailed(error, attempt_id=claim.attempt_id)
    if attempt is None:  # pragma: no cover - the row was just updated
        raise DeliveryPersistenceError("The delivery attempt could not be read back.")
    return SendResult(outcome="sent", attempt=attempt)


def resolve_delivery(
    session: Session,
    draft_id: int,
    *,
    delivered: bool,
    note: str | None,
    now: datetime | None = None,
) -> EmailSendAttempt:
    """Record an operator's verified outcome for an uncertain or interrupted send.

    Allowed when the draft is ``delivery_uncertain``, or ``sending`` with a
    claim older than :data:`STALE_SENDING_AFTER` (the process likely died
    mid-delivery). ``delivered=True`` marks the draft ``sent``; ``False``
    returns it to ``approved`` so it may be sent again. The resolution is
    stored as its own ``manual_resolution`` attempt.
    """
    current = now or datetime.now(UTC)
    try:
        draft = _lock_draft(session, draft_id)
        if draft.state == "sending":
            claim = _latest_attempt(session, draft_id, outcome="in_progress")
            if claim is not None and current - claim.started_at < STALE_SENDING_AFTER:
                raise DeliveryInProgressError(
                    f"Draft {draft_id} is being delivered; wait for the result."
                )
            if claim is not None:
                claim.outcome = "uncertain"
                claim.failure_category = "interrupted"
                claim.finished_at = current
        elif draft.state != "delivery_uncertain":
            raise DeliveryNotResolvableError(
                f"Draft {draft_id} is '{draft.state}'; only an uncertain or interrupted "
                "delivery can be resolved."
            )
        approval = get_approval(session, draft_id)
        if approval is None:  # pragma: no cover - guarded by the state machine
            raise DraftNotApprovedError(f"Draft {draft_id} has no recorded approval.")
        resolution = EmailSendAttempt(
            draft_id=draft_id,
            approval_id=approval.id,
            mode="manual_resolution",
            provider="manual",
            outcome="sent" if delivered else "failed",
            failure_category=None if delivered else "confirmed_not_delivered",
            note=note,
            started_at=current,
            finished_at=current,
            duration_ms=0,
        )
        session.add(resolution)
        draft.state = "sent" if delivered else "approved"
        session.commit()
    except (
        DraftNotFoundError,
        DeliveryInProgressError,
        DeliveryNotResolvableError,
        DraftNotApprovedError,
    ):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("Delivery resolution failed for draft %s", draft_id)
        raise DeliveryPersistenceError("The resolution could not be recorded.") from exc
    session.refresh(resolution)
    logger.info("Draft %s delivery resolved manually: delivered=%s", draft_id, delivered)
    return resolution


def send_attempts(session: Session, draft_id: int) -> list[EmailSendAttempt]:
    return list(
        session.execute(
            select(EmailSendAttempt)
            .where(EmailSendAttempt.draft_id == draft_id)
            .order_by(EmailSendAttempt.started_at.asc(), EmailSendAttempt.id.asc())
        ).scalars()
    )


def draft_detail(session: Session, draft: OutreachDraft) -> DraftDetail:
    """A draft with its approval and full delivery audit trail."""
    approval = get_approval(session, draft.id)
    return DraftDetail(
        **DraftRecord.model_validate(draft, from_attributes=True).model_dump(),
        approval=(
            ApprovalRecord.model_validate(approval, from_attributes=True)
            if approval is not None
            else None
        ),
        send_attempts=[
            SendAttemptRecord.model_validate(attempt, from_attributes=True)
            for attempt in send_attempts(session, draft.id)
        ],
    )
