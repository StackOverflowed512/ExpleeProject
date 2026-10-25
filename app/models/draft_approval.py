"""ORM model for the recorded human approval of an outreach draft."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DraftApproval(Base):
    """An explicit approval binding one draft revision to a recipient and content.

    At most one approval exists per draft (unique ``draft_id``). It is never
    updated: a different recipient or message needs a new draft revision.
    ``content_sha256`` covers the draft ID, revision, recipient, subject, and
    body, so delivery can verify that exactly the approved message is sent.

    ``approved_by`` is a configured local label (``APPROVAL_ACTOR``). The app
    has no authentication, so it is not a verified identity.
    """

    __tablename__ = "draft_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_drafts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False)
    content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 of draft id, revision, recipient, subject, and body.",
    )
    approved_by: Mapped[str] = mapped_column(String(100), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("draft_revision >= 1", name="draft_revision_positive"),
        CheckConstraint("length(content_sha256) = 64", name="content_sha256_length"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DraftApproval(id={self.id!r}, draft_id={self.draft_id!r})"
