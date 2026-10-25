"""Add draft approvals, email send attempts, and the delivery state machine.

Revision ID: 0007_approval_delivery
Revises: 0006_check_constraint_names
Create Date: 2026-10-24

* ``draft_approvals``: one recorded approval per draft (recipient plus a
  SHA-256 over draft id, revision, recipient, subject, and body).
* ``email_send_attempts``: the delivery audit trail (dry runs, real
  attempts, manual resolutions); at most one ``in_progress`` row per draft.
* ``outreach_drafts.state`` widens from ``pending`` to ``pending``,
  ``approved``, ``sending``, ``sent``, ``delivery_uncertain``, plus a
  partial unique index allowing at most one in-flight, sent, or possibly
  sent draft per campaign prospect.

Existing Phase 4 drafts stay ``pending`` with their audit metadata. The
downgrade restores the ``pending``-only constraint; it fails (and rolls
back) if any draft has left ``pending``, rather than rewriting history.
CHECK constraint names use ``op.f()`` so they match the models exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_approval_delivery"
down_revision: str | None = "0006_check_constraint_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DRAFT_STATES = "state IN ('pending', 'approved', 'sending', 'sent', 'delivery_uncertain')"
_DELIVERY_STATES = "state IN ('sending', 'sent', 'delivery_uncertain')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_outreach_drafts_state_valid"), "outreach_drafts", type_="check")
    op.create_check_constraint(
        op.f("ck_outreach_drafts_state_valid"), "outreach_drafts", _DRAFT_STATES
    )
    op.create_index(
        "uq_outreach_drafts_one_delivery_per_prospect",
        "outreach_drafts",
        ["campaign_id", "saved_prospect_id"],
        unique=True,
        postgresql_where=sa.text(_DELIVERY_STATES),
    )

    op.create_table(
        "draft_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.String(length=254), nullable=False),
        sa.Column(
            "content_sha256",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of draft id, revision, recipient, subject, and body.",
        ),
        sa.Column("approved_by", sa.String(length=100), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "draft_revision >= 1", name=op.f("ck_draft_approvals_draft_revision_positive")
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name=op.f("ck_draft_approvals_content_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["outreach_drafts.id"],
            name=op.f("fk_draft_approvals_draft_id_outreach_drafts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_approvals")),
        sa.UniqueConstraint("draft_id", name=op.f("uq_draft_approvals_draft_id")),
    )

    op.create_table(
        "email_send_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_category", sa.String(length=50), nullable=True),
        sa.Column("message_id", sa.String(length=255), nullable=True),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column(
            "note",
            sa.String(length=500),
            nullable=True,
            comment="Operator note for manual resolutions.",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
            "(outcome = 'in_progress') = (finished_at IS NULL)",
            name=op.f("ck_email_send_attempts_finished_matches_outcome"),
        ),
        sa.CheckConstraint(
            "(outcome IN ('failed', 'uncertain')) = (failure_category IS NOT NULL)",
            name=op.f("ck_email_send_attempts_failure_category_matches_outcome"),
        ),
        sa.CheckConstraint(
            "mode <> 'dry_run' OR outcome = 'dry_run'",
            name=op.f("ck_email_send_attempts_dry_run_outcome"),
        ),
        sa.CheckConstraint(
            "mode IN ('dry_run', 'real', 'manual_resolution')",
            name=op.f("ck_email_send_attempts_mode_valid"),
        ),
        sa.CheckConstraint(
            "outcome IN ('in_progress', 'dry_run', 'sent', 'failed', 'uncertain')",
            name=op.f("ck_email_send_attempts_outcome_valid"),
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_email_send_attempts_duration_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["draft_approvals.id"],
            name=op.f("fk_email_send_attempts_approval_id_draft_approvals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["outreach_drafts.id"],
            name=op.f("fk_email_send_attempts_draft_id_outreach_drafts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_send_attempts")),
    )
    op.create_index(
        "ix_email_send_attempts_draft_started",
        "email_send_attempts",
        ["draft_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_email_send_attempts_one_in_progress",
        "email_send_attempts",
        ["draft_id"],
        unique=True,
        postgresql_where=sa.text("outcome = 'in_progress'"),
    )


def downgrade() -> None:
    op.drop_index("uq_email_send_attempts_one_in_progress", table_name="email_send_attempts")
    op.drop_index("ix_email_send_attempts_draft_started", table_name="email_send_attempts")
    op.drop_table("email_send_attempts")
    op.drop_table("draft_approvals")
    op.drop_index(
        "uq_outreach_drafts_one_delivery_per_prospect", table_name="outreach_drafts"
    )
    op.drop_constraint(op.f("ck_outreach_drafts_state_valid"), "outreach_drafts", type_="check")
    # Fails if any draft is not 'pending'; the whole downgrade then rolls back.
    op.create_check_constraint(
        op.f("ck_outreach_drafts_state_valid"), "outreach_drafts", "state IN ('pending')"
    )
