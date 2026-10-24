"""ORM models for outreach campaigns and their selected prospects."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

CAMPAIGN_TONES = ("professional", "friendly", "concise", "formal")


class Campaign(Base):
    """An outreach campaign: what is offered, to whom, and how to ask.

    The configuration fields feed the versioned draft prompt. Campaigns only
    contain saved prospects (see :class:`CampaignProspect`).
    """

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    product: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Product or service being promoted."
    )
    value_proposition: Mapped[str] = mapped_column(Text, nullable=False)
    target_audience: Mapped[str] = mapped_column(String(500), nullable=False)
    call_to_action: Mapped[str] = mapped_column(String(300), nullable=False)
    tone: Mapped[str] = mapped_column(String(30), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sender_company: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
            "tone IN ('professional', 'friendly', 'concise', 'formal')",
            name="tone_valid",
        ),
        Index("ix_campaigns_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Campaign(id={self.id!r}, name={self.name!r})"


class CampaignProspect(Base):
    """A saved prospect selected for a campaign.

    The composite primary key prevents adding the same prospect twice;
    ``position`` preserves the order the prospects were selected in, so
    campaign details and draft generation are deterministic.
    """

    __tablename__ = "campaign_prospects"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    saved_prospect_id: Mapped[int] = mapped_column(
        ForeignKey("saved_prospects.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "position", name="uq_campaign_prospects_position"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_campaign_prospects_saved_prospect_id", "saved_prospect_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CampaignProspect(campaign_id={self.campaign_id!r}, "
            f"saved_prospect_id={self.saved_prospect_id!r})"
        )
