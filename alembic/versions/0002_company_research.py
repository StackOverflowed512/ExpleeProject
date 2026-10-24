"""Create the company_research table.

Revision ID: 0002_company_research
Revises: 0001_company_candidates
Create Date: 2026-10-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_company_research"
down_revision: str | None = "0001_company_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_research",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column(
            "requested_url",
            sa.Text(),
            nullable=False,
            comment="Candidate website URL that research asked for.",
        ),
        sa.Column(
            "final_url",
            sa.Text(),
            nullable=True,
            comment="Last URL requested after permitted redirects.",
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the fetch was started.",
        ),
        sa.Column("fetcher", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_category", sa.String(length=50), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("page_title", sa.String(length=500), nullable=True),
        sa.Column(
            "extracted_text",
            sa.Text(),
            nullable=True,
            comment="Readable page text, capped by RESEARCH_MAX_TEXT_CHARS.",
        ),
        sa.Column(
            "content_truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether the body or extracted text hit a size limit.",
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
            "outcome IN ('success', 'partial', 'empty', 'failed')",
            name="ck_company_research_outcome_valid",
        ),
        sa.CheckConstraint(
            "(outcome = 'failed') = (failure_category IS NOT NULL)",
            name="ck_company_research_failure_category_matches_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["company_candidates.id"],
            name="fk_company_research_candidate_id_company_candidates",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_company_research"),
    )
    op.create_index(
        "ix_company_research_candidate_fetched",
        "company_research",
        ["candidate_id", "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_research_candidate_fetched",
        table_name="company_research",
    )
    op.drop_table("company_research")
