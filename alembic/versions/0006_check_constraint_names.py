"""Rename doubled CHECK constraint names from migrations 0002-0004.

Revision ID: 0006_check_constraint_names
Revises: 0005_campaigns_drafts
Create Date: 2026-10-24

Migrations 0002-0004 passed already-final CHECK constraint names (such as
``ck_company_research_outcome_valid``) to ``op.create_table``. Alembic
applied the metadata naming convention ``ck_%(table_name)s_%(constraint_name)s``
a second time, so migrated databases got doubled names. PostgreSQL's
63-character limit made SQLAlchemy shorten the longest of them to 55
characters plus ``_`` and four hex digits of their MD5, for example
``ck_company_qualifications_ck_company_qualifications_sta_abd7``. Databases
built with ``Base.metadata.create_all`` have the correct single names.

This revision only renames constraints; their definitions and all data are
unchanged. A rename happens only when the doubled name exists, so the
migration is a no-op where names are already correct. The downgrade restores
the exact doubled names, which migration 0004's downgrade relies on.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text
from sqlalchemy.util import md5_hex

revision: str = "0006_check_constraint_names"
down_revision: str | None = "0005_campaigns_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSTGRES_MAX_IDENTIFIER = 63

# Table -> constraint name suffixes declared by the models (name="...").
AFFECTED = {
    "company_research": ("outcome_valid", "failure_category_matches_outcome"),
    "company_qualifications": (
        "status_valid",
        "score_range",
        "threshold_range",
        "evidence_coverage_range",
    ),
}


def correct_name(table: str, suffix: str) -> str:
    """The name the model's naming convention produces."""
    return f"ck_{table}_{suffix}"


def doubled_name(table: str, suffix: str) -> str:
    """The name migrations 0002-0004 produced (convention applied twice).

    Mirrors SQLAlchemy's identifier truncation for names over the limit:
    the first ``max - 8`` characters, ``_``, and the last four hex digits of
    the MD5 of the full name.
    """
    name = f"ck_{table}_{correct_name(table, suffix)}"
    if len(name) > POSTGRES_MAX_IDENTIFIER:
        name = name[: POSTGRES_MAX_IDENTIFIER - 8] + "_" + md5_hex(name)[-4:]
    return name


def _existing_check_names(table: str) -> set[str]:
    rows = op.get_bind().execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = to_regclass(:table) AND contype = 'c'"
        ),
        {"table": table},
    )
    return {row[0] for row in rows}


def _rename(table: str, old: str, new: str) -> None:
    # Identifiers are module constants, never user input.
    op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def upgrade() -> None:
    for table, suffixes in AFFECTED.items():
        existing = _existing_check_names(table)
        for suffix in suffixes:
            old, new = doubled_name(table, suffix), correct_name(table, suffix)
            if old in existing and new not in existing:
                _rename(table, old, new)


def downgrade() -> None:
    for table, suffixes in AFFECTED.items():
        existing = _existing_check_names(table)
        for suffix in suffixes:
            new, old = correct_name(table, suffix), doubled_name(table, suffix)
            if new in existing and old not in existing:
                _rename(table, new, old)
