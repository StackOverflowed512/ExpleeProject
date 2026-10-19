"""Create the company_candidates table.

Revision ID: 0001_company_candidates
Revises:
Create Date: 2026-10-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_company_candidates"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_result_id", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this candidate was first discovered.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When this candidate row was last updated.",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_company_candidates"),
    )
    op.create_index(
        "uq_company_candidates_domain",
        "company_candidates",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("domain IS NOT NULL"),
    )
    op.create_index(
        "uq_company_candidates_provider_result",
        "company_candidates",
        ["provider", "provider_result_id"],
        unique=True,
        postgresql_where=sa.text("provider_result_id IS NOT NULL"),
    )
    op.create_index(
        "ix_company_candidates_discovered_at",
        "company_candidates",
        ["discovered_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_candidates_discovered_at",
        table_name="company_candidates",
    )
    op.drop_index(
        "uq_company_candidates_provider_result",
        table_name="company_candidates",
    )
    op.drop_index(
        "uq_company_candidates_domain",
        table_name="company_candidates",
    )
    op.drop_table("company_candidates")
