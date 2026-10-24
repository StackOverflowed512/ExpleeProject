"""Allow the 'preliminary' qualification status (qualification-rules/v2).

Revision ID: 0004_qualification_preliminary
Revises: 0003_qualification_prospects
Create Date: 2026-10-24

Replaces only the status check constraint on ``company_qualifications``.
Existing rows are unchanged (they all satisfy both versions). Downgrading
fails if any 'preliminary' rows exist, which is safer than rewriting
recorded history; delete or relabel them deliberately first.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_qualification_preliminary"
down_revision: str | None = "0003_qualification_prospects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "company_qualifications"
_NAME = "ck_company_qualifications_status_valid"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _NAME,
        _TABLE,
        "status IN ('qualified', 'preliminary', 'unqualified', 'insufficient_evidence')",
    )


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _NAME,
        _TABLE,
        "status IN ('qualified', 'unqualified', 'insufficient_evidence')",
    )
