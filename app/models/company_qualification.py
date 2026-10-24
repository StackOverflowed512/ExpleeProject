"""ORM model for qualification runs on discovered companies."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompanyQualification(Base):
    """One deterministic qualification run for a :class:`CompanyCandidate`.

    Append-only: every run is a new row and rows are never updated or
    pruned, so the full decision history stays auditable. ``criteria`` is the
    validated profile that was applied, ``factors`` holds each factor's
    result, weight, points, signals, and sourced evidence, and
    ``rule_version`` identifies the scoring rules that produced them.
    ``research_id`` references the research used as evidence; it becomes
    ``NULL`` if research history pruning later removes that row, while the
    evidence excerpts in ``factors`` remain.
    """

    __tablename__ = "company_qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("company_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    research_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_research.id", ondelete="SET NULL"),
        nullable=True,
        comment="Research row used as evidence, if any.",
    )
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_coverage: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Evaluated factor weight / applicable factor weight (0-1).",
    )
    factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the rules were evaluated.",
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
            "status IN ('qualified', 'unqualified', 'insufficient_evidence')",
            name="status_valid",
        ),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        CheckConstraint(
            "evidence_coverage >= 0 AND evidence_coverage <= 1",
            name="evidence_coverage_range",
        ),
        Index(
            "ix_company_qualifications_candidate_qualified",
            "candidate_id",
            "qualified_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CompanyQualification(id={self.id!r}, candidate_id={self.candidate_id!r}, "
            f"status={self.status!r}, score={self.score!r})"
        )
