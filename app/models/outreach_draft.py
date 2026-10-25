"""ORM model for generated outreach drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutreachDraft(Base):
    """One generated email draft for one campaign prospect.

    Drafts are created complete (subject and body validated before insert)
    and are never overwritten: regeneration adds a new ``revision``. The
    composite foreign key to ``campaign_prospects`` guarantees the prospect
    belongs to the campaign.

    ``state`` (Phase 5 state machine, enforced by the delivery service):
    ``pending`` -> ``approved`` (recorded approval) -> ``sending`` (claimed
    for one real delivery) -> ``sent`` | ``approved`` (definite failure,
    safe to retry) | ``delivery_uncertain`` (may have been delivered; needs
    an explicit resolution). Dry runs never change the state.

    Audit fields: ``prompt_version``, ``prompt_sha256`` (hash of the exact
    messages sent), ``provider``, ``model``, ``generation_metadata`` (safe
    provider facts such as response ID, finish reason, token usage), and
    ``provenance`` (candidate, qualification, research, and evidence sources
    the prompt used).
    """

    __tablename__ = "outreach_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(Integer, nullable=False)
    saved_prospect_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    qualification_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_qualifications.id", ondelete="SET NULL"),
        nullable=True,
        comment="Qualification whose evidence informed the prompt.",
    )
    research_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_research.id", ondelete="SET NULL"),
        nullable=True,
        comment="Research row whose text informed the prompt, if any.",
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
        ForeignKeyConstraint(
            ["campaign_id", "saved_prospect_id"],
            ["campaign_prospects.campaign_id", "campaign_prospects.saved_prospect_id"],
            name="fk_outreach_drafts_campaign_prospect",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "campaign_id",
            "saved_prospect_id",
            "revision",
            name="uq_outreach_drafts_campaign_prospect_revision",
        ),
        CheckConstraint(
            "state IN ('pending', 'approved', 'sending', 'sent', 'delivery_uncertain')",
            name="state_valid",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("length(btrim(subject)) > 0", name="subject_not_blank"),
        CheckConstraint("length(btrim(body)) > 0", name="body_not_blank"),
        Index("ix_outreach_drafts_saved_prospect_id", "saved_prospect_id"),
        # At most one draft per campaign prospect may be in flight, sent, or
        # possibly sent, so two revisions can never both email the prospect.
        Index(
            "uq_outreach_drafts_one_delivery_per_prospect",
            "campaign_id",
            "saved_prospect_id",
            unique=True,
            postgresql_where=text(
                "state IN ('sending', 'sent', 'delivery_uncertain')"
            ),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"OutreachDraft(id={self.id!r}, campaign_id={self.campaign_id!r}, "
            f"saved_prospect_id={self.saved_prospect_id!r}, revision={self.revision!r})"
        )
