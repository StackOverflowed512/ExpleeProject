"""Create company_qualifications and saved_prospects.

Revision ID: 0003_qualification_prospects
Revises: 0002_company_research
Create Date: 2026-10-24

Additive only: no existing table, column, or row is changed, so Phase 1
candidates and Phase 2 research survive the upgrade unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_qualification_prospects"
down_revision: str | None = "0002_company_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_qualifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column(
            "research_id",
            sa.Integer(),
            nullable=True,
            comment="Research row used as evidence, if any.",
        ),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column(
            "evidence_coverage",
            sa.Float(),
            nullable=False,
            comment="Evaluated factor weight / applicable factor weight (0-1).",
        ),
        sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "qualified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the rules were evaluated.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('qualified', 'unqualified', 'insufficient_evidence')",
            name="ck_company_qualifications_status_valid",
        ),
        sa.CheckConstraint(
            "score BETWEEN 0 AND 100",
            name="ck_company_qualifications_score_range",
        ),
        sa.CheckConstraint(
            "threshold BETWEEN 1 AND 100",
            name="ck_company_qualifications_threshold_range",
        ),
        sa.CheckConstraint(
            "evidence_coverage >= 0 AND evidence_coverage <= 1",
            name="ck_company_qualifications_evidence_coverage_range",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["company_candidates.id"],
            name="fk_company_qualifications_candidate_id_company_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_id"],
            ["company_research.id"],
            name="fk_company_qualifications_research_id_company_research",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_company_qualifications"),
    )
    op.create_index(
        "ix_company_qualifications_candidate_qualified",
        "company_qualifications",
        ["candidate_id", "qualified_at"],
        unique=False,
    )

    op.create_table(
        "saved_prospects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("qualification_id", sa.Integer(), nullable=False),
        sa.Column(
            "saved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["company_candidates.id"],
            name="fk_saved_prospects_candidate_id_company_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["qualification_id"],
            ["company_qualifications.id"],
            name="fk_saved_prospects_qualification_id_company_qualifications",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_prospects"),
        sa.UniqueConstraint("candidate_id", name="uq_saved_prospects_candidate_id"),
    )
    op.create_index(
        "ix_saved_prospects_saved_at",
        "saved_prospects",
        ["saved_at"],
        unique=False,
    )
    op.create_index(
        "ix_saved_prospects_qualification_id",
        "saved_prospects",
        ["qualification_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saved_prospects_qualification_id", table_name="saved_prospects")
    op.drop_index("ix_saved_prospects_saved_at", table_name="saved_prospects")
    op.drop_table("saved_prospects")
    op.drop_index(
        "ix_company_qualifications_candidate_qualified",
        table_name="company_qualifications",
    )
    op.drop_table("company_qualifications")
