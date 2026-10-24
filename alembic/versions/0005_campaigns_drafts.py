"""Create campaigns, campaign_prospects, and outreach_drafts.

Revision ID: 0005_campaigns_drafts
Revises: 0004_qualification_preliminary
Create Date: 2026-10-24

Additive only: no existing table, column, or row is changed, so candidates,
research, qualifications, and saved prospects survive the upgrade unchanged.
Draft ``state`` is limited to 'pending'; Phase 5 widens it for approval.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_campaigns_drafts"
down_revision: str | None = "0004_qualification_preliminary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# CHECK constraint names are wrapped in op.f(): the "ck" naming convention
# embeds %(constraint_name)s, so Alembic would otherwise prefix the already
# final name again (ck_<table>_ck_<table>_...). Migrations 0002-0004 created
# their CHECK constraints with such doubled names; they are left unchanged.
def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "product",
            sa.String(length=200),
            nullable=False,
            comment="Product or service being promoted.",
        ),
        sa.Column("value_proposition", sa.Text(), nullable=False),
        sa.Column("target_audience", sa.String(length=500), nullable=False),
        sa.Column("call_to_action", sa.String(length=300), nullable=False),
        sa.Column("tone", sa.String(length=30), nullable=False),
        sa.Column("sender_name", sa.String(length=100), nullable=True),
        sa.Column("sender_company", sa.String(length=200), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "tone IN ('professional', 'friendly', 'concise', 'formal')",
            name=op.f("ck_campaigns_tone_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaigns"),
    )
    op.create_index("ix_campaigns_created_at", "campaigns", ["created_at"], unique=False)

    op.create_table(
        "campaign_prospects",
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("saved_prospect_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_campaign_prospects_position_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_prospects_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["saved_prospect_id"],
            ["saved_prospects.id"],
            name="fk_campaign_prospects_saved_prospect_id_saved_prospects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "campaign_id", "saved_prospect_id", name="pk_campaign_prospects"
        ),
        sa.UniqueConstraint(
            "campaign_id", "position", name="uq_campaign_prospects_position"
        ),
    )
    op.create_index(
        "ix_campaign_prospects_saved_prospect_id",
        "campaign_prospects",
        ["saved_prospect_id"],
        unique=False,
    )

    op.create_table(
        "outreach_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("saved_prospect_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "state", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column(
            "generation_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "qualification_id",
            sa.Integer(),
            nullable=True,
            comment="Qualification whose evidence informed the prompt.",
        ),
        sa.Column(
            "research_id",
            sa.Integer(),
            nullable=True,
            comment="Research row whose text informed the prompt, if any.",
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("state IN ('pending')", name=op.f("ck_outreach_drafts_state_valid")),
        sa.CheckConstraint(
            "length(btrim(body)) > 0", name=op.f("ck_outreach_drafts_body_not_blank")
        ),
        sa.CheckConstraint(
            "length(btrim(subject)) > 0", name=op.f("ck_outreach_drafts_subject_not_blank")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_outreach_drafts_revision_positive")),
        sa.ForeignKeyConstraint(
            ["campaign_id", "saved_prospect_id"],
            ["campaign_prospects.campaign_id", "campaign_prospects.saved_prospect_id"],
            name="fk_outreach_drafts_campaign_prospect",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["qualification_id"],
            ["company_qualifications.id"],
            name="fk_outreach_drafts_qualification_id_company_qualifications",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["research_id"],
            ["company_research.id"],
            name="fk_outreach_drafts_research_id_company_research",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outreach_drafts"),
        sa.UniqueConstraint(
            "campaign_id",
            "saved_prospect_id",
            "revision",
            name="uq_outreach_drafts_campaign_prospect_revision",
        ),
    )
    op.create_index(
        "ix_outreach_drafts_saved_prospect_id",
        "outreach_drafts",
        ["saved_prospect_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outreach_drafts_saved_prospect_id", table_name="outreach_drafts")
    op.drop_table("outreach_drafts")
    op.drop_index(
        "ix_campaign_prospects_saved_prospect_id", table_name="campaign_prospects"
    )
    op.drop_table("campaign_prospects")
    op.drop_index("ix_campaigns_created_at", table_name="campaigns")
    op.drop_table("campaigns")
