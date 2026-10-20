"""ORM model for stored website research attempts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ResearchRecord(Base):
    """One auditable website research attempt for a company candidate.

    Records are append-only: a successful record is never overwritten by a
    later failure, and a changed page creates a new record. ``attempt_key``
    chains an attempt to the previous record and a content/outcome signature,
    which makes an identical consecutive attempt idempotent while still
    recording real state changes.
    """

    __tablename__ = "research_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("company_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the website fetch completed.",
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    page_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        comment="Whether extracted text was capped by the configured limit.",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 of extracted title/text for successful attempts.",
    )
    attempt_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 idempotency key chained to the previous record.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When this research attempt was stored.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="When this research row was last updated.",
    )

    __table_args__ = (
        UniqueConstraint(
            "company_candidate_id",
            "attempt_key",
            name="uq_research_records_candidate_attempt",
        ),
        Index(
            "ix_research_records_candidate_fetched",
            "company_candidate_id",
            "fetched_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            "ResearchRecord("
            f"id={self.id!r}, "
            f"company_candidate_id={self.company_candidate_id!r}, "
            f"outcome={self.outcome!r})"
        )
