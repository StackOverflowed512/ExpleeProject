"""Recorded human approval of outreach drafts.

Approval is the gate in front of delivery. It is enforced here, not in a
UI, and it binds one exact draft revision to one explicitly supplied
recipient through :func:`content_sha256`.

Rules:

* The draft must exist and its ``revision`` must equal the requested one.
* Only a ``pending`` draft can be approved. Approval moves it to ``approved``
  in the same transaction that records the approval.
* At most one approval exists per draft. Repeating an identical request
  returns the existing approval (``created=False``). A request with a
  different recipient is refused; to change the recipient or the message,
  generate a new draft revision and approve that.

The application has no authentication. ``approved_by`` records the
configured ``APPROVAL_ACTOR`` label, which is not a verified identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.draft_approval import DraftApproval
from app.models.outreach_draft import OutreachDraft
from app.services.drafts import DraftNotFoundError

logger = logging.getLogger(__name__)


class DraftRevisionMismatchError(Exception):
    """The requested revision is not the draft's revision."""


class DraftAlreadyApprovedError(Exception):
    """The draft is already approved for a different recipient."""


class DraftNotApprovableError(Exception):
    """The draft is not in a state that can be approved."""


class ApprovalPersistenceError(Exception):
    """The approval could not be saved."""


@dataclass(frozen=True)
class ApprovalResult:
    approval: DraftApproval
    created: bool


def content_sha256(
    *, draft_id: int, revision: int, recipient: str, subject: str, body: str
) -> str:
    """Stable hash binding an approval to the exact message and recipient."""
    material = json.dumps(
        {
            "body": body,
            "draft_id": draft_id,
            "recipient": recipient,
            "revision": revision,
            "subject": subject,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def approval_matches(approval: DraftApproval, draft: OutreachDraft) -> bool:
    """Whether ``approval`` still covers exactly this draft's current content."""
    return approval.draft_id == draft.id and approval.draft_revision == draft.revision and (
        approval.content_sha256
        == content_sha256(
            draft_id=draft.id,
            revision=draft.revision,
            recipient=approval.recipient_email,
            subject=draft.subject,
            body=draft.body,
        )
    )


def get_approval(session: Session, draft_id: int) -> DraftApproval | None:
    return session.execute(
        select(DraftApproval).where(DraftApproval.draft_id == draft_id)
    ).scalar_one_or_none()


def _existing_result(
    approval: DraftApproval, *, recipient: str, revision: int
) -> ApprovalResult:
    if approval.recipient_email != recipient:
        raise DraftAlreadyApprovedError(
            f"Draft {approval.draft_id} is already approved for a different recipient; "
            "generate a new draft revision to send to someone else."
        )
    if approval.draft_revision != revision:
        raise DraftRevisionMismatchError(
            f"Draft {approval.draft_id} is revision {approval.draft_revision}, not {revision}."
        )
    return ApprovalResult(approval=approval, created=False)


def approve_draft(
    session: Session,
    draft_id: int,
    *,
    recipient: str,
    revision: int,
    actor: str,
    now: datetime | None = None,
) -> ApprovalResult:
    """Record approval of one draft revision for ``recipient`` (already validated)."""
    try:
        draft = session.execute(
            select(OutreachDraft).where(OutreachDraft.id == draft_id).with_for_update()
        ).scalar_one_or_none()
        if draft is None:
            raise DraftNotFoundError(f"Draft {draft_id} was not found.")

        existing = get_approval(session, draft_id)
        if existing is not None:
            result = _existing_result(existing, recipient=recipient, revision=revision)
            session.rollback()
            return result
        if draft.revision != revision:
            raise DraftRevisionMismatchError(
                f"Draft {draft_id} is revision {draft.revision}, not {revision}."
            )
        if draft.state != "pending":
            raise DraftNotApprovableError(
                f"Draft {draft_id} is '{draft.state}' and cannot be approved."
            )

        approval = DraftApproval(
            draft_id=draft.id,
            draft_revision=draft.revision,
            recipient_email=recipient,
            content_sha256=content_sha256(
                draft_id=draft.id,
                revision=draft.revision,
                recipient=recipient,
                subject=draft.subject,
                body=draft.body,
            ),
            approved_by=actor,
            approved_at=now or datetime.now(UTC),
        )
        session.add(approval)
        draft.state = "approved"
        session.commit()
    except (
        DraftNotFoundError,
        DraftRevisionMismatchError,
        DraftNotApprovableError,
        DraftAlreadyApprovedError,
    ):
        session.rollback()
        raise
    except IntegrityError:
        # A concurrent request approved this draft first.
        session.rollback()
        existing = get_approval(session, draft_id)
        if existing is None:  # pragma: no cover - defensive
            raise ApprovalPersistenceError("The approval could not be saved.") from None
        result = _existing_result(existing, recipient=recipient, revision=revision)
        session.rollback()
        return result
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("Approval persistence failed for draft %s", draft_id)
        raise ApprovalPersistenceError("The approval could not be saved.") from exc

    session.refresh(approval)
    logger.info("Draft %s approved by %s", draft_id, actor)
    return ApprovalResult(approval=approval, created=True)
