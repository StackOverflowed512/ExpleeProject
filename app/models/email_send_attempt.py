"""ORM model for email send attempts (the delivery audit trail)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

ATTEMPT_MODES = ("dry_run", "real", "manual_resolution")
ATTEMPT_OUTCOMES = ("in_progress", "dry_run", "sent", "failed", "uncertain")


class EmailSendAttempt(Base):
    """One send attempt, dry run, or manual resolution for an approved draft.

    Rows are never deleted. A real attempt is inserted as ``in_progress``
    when the draft is claimed and finalized exactly once (``sent``,
    ``failed``, or ``uncertain``). Dry runs and manual resolutions are
    inserted already final. At most one attempt per draft can be
    ``in_progress`` (partial unique index).

    ``message_id`` is the Message-ID header used for the attempt;
    ``provider_reference`` is the sanitized server reply (for example a
    queue ID). No credentials, raw exceptions, or message content are stored.
    """

    __tablename__ = "email_send_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_drafts.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[int] = mapped_column(
        ForeignKey("draft_approvals.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Operator note for manual resolutions."
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('dry_run', 'real', 'manual_resolution')", name="mode_valid"
        ),
        CheckConstraint(
            "outcome IN ('in_progress', 'dry_run', 'sent', 'failed', 'uncertain')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(outcome = 'in_progress') = (finished_at IS NULL)",
            name="finished_matches_outcome",
        ),
        CheckConstraint(
            "(outcome IN ('failed', 'uncertain')) = (failure_category IS NOT NULL)",
            name="failure_category_matches_outcome",
        ),
        CheckConstraint(
            "mode <> 'dry_run' OR outcome = 'dry_run'", name="dry_run_outcome"
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
        Index("ix_email_send_attempts_draft_started", "draft_id", "started_at"),
        Index(
            "uq_email_send_attempts_one_in_progress",
            "draft_id",
            unique=True,
            postgresql_where=text("outcome = 'in_progress'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"EmailSendAttempt(id={self.id!r}, draft_id={self.draft_id!r}, "
            f"mode={self.mode!r}, outcome={self.outcome!r})"
        )
