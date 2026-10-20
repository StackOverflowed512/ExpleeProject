"""Create the research_records table.

Revision ID: 0002_research_records
Revises: 0001_company_candidates
Create Date: 2026-10-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_research_records"
down_revision: str | None = "0001_company_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_candidate_id", sa.Integer(), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the website fetch completed.",
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("page_title", sa.Text(), nullable=True),
        sa.Column("page_text", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_category", sa.String(length=50), nullable=True),
        sa.Column(
            "truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether extracted text was capped by the configured limit.",
        ),
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 of extracted title/text for successful attempts.",
        ),
        sa.Column(
            "attempt_key",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 idempotency key chained to the previous record.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this research attempt was stored.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this research row was last updated.",
        ),
        sa.ForeignKeyConstraint(
            ["company_candidate_id"],
            ["company_candidates.id"],
            name="fk_research_records_company_candidate_id_company_candidates",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_records"),
        sa.UniqueConstraint(
            "company_candidate_id",
            "attempt_key",
            name="uq_research_records_candidate_attempt",
        ),
    )
    op.create_index(
        "ix_research_records_candidate_fetched",
        "research_records",
        ["company_candidate_id", "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_records_candidate_fetched",
        table_name="research_records",
    )
    op.drop_table("research_records")
