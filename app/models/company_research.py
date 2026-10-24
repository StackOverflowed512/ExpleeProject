"""ORM model for website research attempts on discovered companies."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompanyResearch(Base):
    """One website research attempt for a :class:`CompanyCandidate`.

    History policy: every attempt is a new row, so a failed attempt never
    overwrites earlier research. The research service prunes old attempts
    per candidate (keeping the most recent ones and always the latest usable
    record), which bounds growth. Provenance is the ``requested_url`` (taken
    from the candidate), the ``final_url`` after redirects, ``fetched_at``,
    and the ``fetcher`` that produced the row. Discovery provenance on the
    candidate (``source_url``, ``provider``) is never modified.
    """

    __tablename__ = "company_research"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("company_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Candidate website URL that research asked for.",
    )
    final_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Last URL requested after permitted redirects.",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the fetch was started.",
    )
    fetcher: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Readable page text, capped by RESEARCH_MAX_TEXT_CHARS.",
    )
    content_truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="Whether the body or extracted text hit a size limit.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'partial', 'empty', 'failed')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(outcome = 'failed') = (failure_category IS NOT NULL)",
            name="failure_category_matches_outcome",
        ),
        Index(
            "ix_company_research_candidate_fetched",
            "candidate_id",
            "fetched_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CompanyResearch(id={self.id!r}, candidate_id={self.candidate_id!r}, "
            f"outcome={self.outcome!r})"
        )
