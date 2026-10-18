"""ORM model for companies discovered through web search."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompanyCandidate(Base):
    """A company discovered by an Explorer search provider.

    Deduplication is based on normalized ``domain`` when known, otherwise on
    ``(provider, provider_result_id)``. Candidates with neither are kept as
    separate rows; names are deliberately not part of any uniqueness rule, so
    distinct companies with similar names are never merged.
    """

    __tablename__ = "company_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_result_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When this candidate was first discovered.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="When this candidate row was last updated.",
    )

    __table_args__ = (
        Index(
            "uq_company_candidates_domain",
            "domain",
            unique=True,
            postgresql_where=domain.isnot(None),
        ),
        Index(
            "uq_company_candidates_provider_result",
            "provider",
            "provider_result_id",
            unique=True,
            postgresql_where=provider_result_id.isnot(None),
        ),
        Index("ix_company_candidates_discovered_at", "discovered_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CompanyCandidate(id={self.id!r}, name={self.name!r}, domain={self.domain!r})"
