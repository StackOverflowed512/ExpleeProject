"""Outreach draft request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.campaigns import MAX_CAMPAIGN_PROSPECTS, MAX_DB_ID
from app.schemas.text_rules import normalize_email_address

# Phase 5 state machine (see app/services/delivery.py and docs/architecture.md).
DraftState = Literal["pending", "approved", "sending", "sent", "delivery_uncertain"]
SendMode = Literal["dry_run", "deliver"]
AttemptMode = Literal["dry_run", "real", "manual_resolution"]
AttemptOutcome = Literal["in_progress", "dry_run", "sent", "failed", "uncertain"]
SendOutcome = Literal["dry_run", "sent", "already_sent"]
GenerationOutcome = Literal["generated", "existing"]


class GenerateDraftsRequest(BaseModel):
    """Body of ``POST /campaigns/{id}/drafts`` (optional)."""

    model_config = ConfigDict(extra="forbid")

    saved_prospect_ids: (
        Annotated[
            list[Annotated[int, Field(ge=1, le=MAX_DB_ID)]],
            Field(min_length=1, max_length=MAX_CAMPAIGN_PROSPECTS),
        ]
        | None
    ) = Field(
        default=None,
        description="Limit generation to these campaign prospects; omit for all.",
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Create a new revision even for prospects that already have a draft. "
            "Earlier drafts are kept, never overwritten."
        ),
    )

    @field_validator("saved_prospect_ids")
    @classmethod
    def _no_duplicates(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("saved_prospect_ids must not repeat")
        return value


class DraftRecord(BaseModel):
    """A persisted outreach draft with its audit trail."""

    id: int
    campaign_id: int
    saved_prospect_id: int
    revision: int
    state: DraftState
    subject: str
    body: str
    prompt_version: str
    prompt_sha256: str = Field(description="SHA-256 of the exact prompt messages sent.")
    provider: str
    model: str
    generation_metadata: dict[str, Any] = Field(
        description="Safe provider facts: response ID, finish reason, token usage."
    )
    provenance: dict[str, Any] = Field(
        description="Candidate, qualification, research, and evidence sources used."
    )
    qualification_id: int | None = None
    research_id: int | None = None
    generated_at: datetime
    created_at: datetime
    updated_at: datetime


class DraftGenerationItem(BaseModel):
    """What happened for one campaign prospect in a generation request."""

    saved_prospect_id: int
    outcome: GenerationOutcome = Field(
        description="'generated': a new draft was created; 'existing': skipped "
        "because a draft already exists and regenerate was false."
    )
    draft: DraftRecord


class GenerateDraftsResponse(BaseModel):
    """Result of one generation request."""

    campaign_id: int
    batch_limit: int = Field(description="Maximum drafts generated per request.")
    results: list[DraftGenerationItem]
    remaining: int = Field(
        description="Targeted prospects still waiting because of batch_limit; "
        "repeat the request to continue."
    )


class DraftListResponse(BaseModel):
    """One page of a campaign's drafts in campaign order, then revision."""

    items: list[DraftRecord]
    total: int
    limit: int
    offset: int


# -- approval and delivery (Phase 5) ------------------------------------------
class ApproveDraftRequest(BaseModel):
    """Body of ``POST /drafts/{id}/approve``.

    The recipient is always supplied explicitly by the operator; it is never
    guessed or looked up. ``revision`` must equal the draft's revision, which
    confirms the operator approves exactly the version they reviewed.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"recipient_email": "ops@acme.com", "revision": 1}]},
    )

    recipient_email: str = Field(description="Plain address, for example 'ops@acme.com'.")
    revision: int = Field(ge=1, le=MAX_DB_ID)

    @field_validator("recipient_email", mode="before")
    @classmethod
    def _validate_recipient(cls, value: object) -> object:
        return normalize_email_address(value) if isinstance(value, str) else value


class SendDraftRequest(BaseModel):
    """Body of ``POST /drafts/{id}/send``.

    There are deliberately no recipient, subject, or body fields: delivery
    always uses the approved snapshot.
    """

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"mode": "dry_run"}]}
    )

    mode: SendMode = Field(
        description=(
            "'dry_run' records an attempt without contacting any provider; "
            "'deliver' sends one real email (requires EMAIL_DRY_RUN=false)."
        )
    )


class ResolveDeliveryRequest(BaseModel):
    """Body of ``POST /drafts/{id}/resolve-delivery`` (manual recovery)."""

    model_config = ConfigDict(extra="forbid")

    delivered: bool = Field(
        description=(
            "True if the operator confirmed the message arrived (draft becomes "
            "'sent'); false if it confirmed it did not (draft returns to "
            "'approved' and may be sent again)."
        )
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def _clean_note(cls, value: object) -> object:
        if isinstance(value, str):
            if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value):
                raise ValueError("must not contain control characters")
            return value.strip() or None
        return value


class ApprovalRecord(BaseModel):
    """A recorded approval. ``approved_by`` is a local label, not an identity."""

    id: int
    draft_id: int
    draft_revision: int
    recipient_email: str
    content_sha256: str
    approved_by: str
    approved_at: datetime
    created_at: datetime


class SendAttemptRecord(BaseModel):
    """One audited dry run, delivery attempt, or manual resolution."""

    id: int
    draft_id: int
    approval_id: int
    mode: AttemptMode
    provider: str
    outcome: AttemptOutcome
    failure_category: str | None = None
    message_id: str | None = None
    provider_reference: str | None = None
    note: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None


class DraftDetail(DraftRecord):
    """A draft with its approval and full delivery audit trail."""

    approval: ApprovalRecord | None = None
    send_attempts: list[SendAttemptRecord] = Field(
        description="All attempts for this draft, oldest first."
    )


class ApproveDraftResponse(BaseModel):
    """Result of an approval request.

    ``created`` is false when an identical approval already existed.
    """

    created: bool
    draft: DraftDetail


class SendDraftResponse(BaseModel):
    """Result of a send request.

    ``already_sent`` means the draft had been delivered before; nothing was
    sent again and ``attempt`` is the earlier successful attempt.
    """

    outcome: SendOutcome
    attempt: SendAttemptRecord
    draft: DraftDetail
