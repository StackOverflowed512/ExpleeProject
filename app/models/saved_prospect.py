"""ORM model for saved prospects."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SavedProspect(Base):
    """A qualified candidate saved for later outreach.

    At most one prospect exists per candidate (unique ``candidate_id``).
    ``qualification_id`` is the persisted qualification that justified the
    save; the prospects service only accepts a qualification with status
    ``qualified`` that belongs to the same candidate. Company and research
    data are not copied here; they are read from the candidate and its
    history.
    """

    __tablename__ = "saved_prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("company_candidates.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # ON DELETE NO ACTION (stated explicitly): a qualification that justifies
    # a prospect cannot be deleted on its own. NO ACTION rather than RESTRICT
    # because the check runs at the end of the statement, so deleting the
    # candidate can cascade to both the prospect and its qualification.
    qualification_id: Mapped[int] = mapped_column(
        ForeignKey("company_qualifications.id", ondelete="NO ACTION"),
        nullable=False,
    )
    saved_at: Mapped[datetime] = mapped_column(
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
        Index("ix_saved_prospects_saved_at", "saved_at"),
        Index("ix_saved_prospects_qualification_id", "qualification_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SavedProspect(id={self.id!r}, candidate_id={self.candidate_id!r}, "
            f"qualification_id={self.qualification_id!r})"
        )
