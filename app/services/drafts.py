"""Outreach draft generation, persistence, and retrieval.

Flow for ``POST /campaigns/{id}/drafts``:

1. A short read-only session loads the campaign, its prospects (in campaign
   order), existing drafts, and the evidence for each prospect, and builds
   one versioned prompt per prospect to generate. It then closes.
2. For each prospect, the injected :class:`DraftGenerator` is called with
   **no** database session open. Its output is fully validated before
   anything is written.
3. Each validated draft is inserted in its own narrow transaction. A
   provider or validation failure therefore never leaves a partial draft,
   and drafts already saved by the same request stay saved.

Repeat behaviour. Without ``regenerate``, prospects that already have a
draft are skipped (reported as ``existing``), so retrying a request never
duplicates drafts. With ``regenerate``, a new ``revision`` is added;
earlier drafts are kept. A unique constraint on
``(campaign_id, saved_prospect_id, revision)`` plus ``ON CONFLICT DO
NOTHING`` resolves concurrent requests to a single row. At most
:data:`BATCH_LIMIT` drafts are generated per request; the response reports
how many targeted prospects remain.

Nothing here sends email. Drafts are created in state ``pending``; Phase 5
adds the approval gate and delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models.campaign import Campaign, CampaignProspect
from app.models.outreach_draft import OutreachDraft
from app.schemas.drafts import DraftRecord, GenerationOutcome
from app.schemas.research import EnrichedCompany
from app.services.campaigns import CampaignProspectRow, campaign_prospect_rows, get_campaign
from app.services.enrichment import get_enrichment
from app.services.outreach_prompt import (
    CampaignBrief,
    ProspectFacts,
    build_prompt,
    prompt_sha256,
)
from app.services.providers.drafts import (
    DraftGenerator,
    DraftGeneratorError,
    DraftPrompt,
)
from app.services.qualification import to_qualification_record

logger = logging.getLogger(__name__)

BATCH_LIMIT = 10
_EVIDENCE_FACTORS = ("industry_fit", "location_fit")


class DraftNotFoundError(Exception):
    """No draft exists with the requested ID."""


class ProspectNotInCampaignError(Exception):
    """Some requested saved prospects are not part of the campaign."""

    def __init__(self, missing: list[int]) -> None:
        super().__init__(
            "These saved prospects are not in the campaign: "
            + ", ".join(str(pid) for pid in missing)
        )
        self.missing = missing


class DraftPersistenceError(Exception):
    """A validated draft could not be saved."""


class DraftGenerationFailed(Exception):
    """The generator failed partway through a request.

    ``cause`` is the provider error (safe message and stable code).
    ``generated_draft_ids`` lists drafts saved by this request before the
    failure; they are complete and remain valid.
    """

    def __init__(
        self,
        cause: DraftGeneratorError,
        *,
        failed_saved_prospect_id: int,
        generated_draft_ids: list[int],
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.failed_saved_prospect_id = failed_saved_prospect_id
        self.generated_draft_ids = generated_draft_ids


@dataclass(frozen=True)
class GenerationItem:
    saved_prospect_id: int
    outcome: GenerationOutcome
    draft: OutreachDraft


@dataclass(frozen=True)
class GenerationResult:
    campaign_id: int
    items: list[GenerationItem]
    remaining: int


@dataclass(frozen=True)
class _Plan:
    saved_prospect_id: int
    prompt: DraftPrompt
    provenance: dict[str, Any]
    qualification_id: int
    research_id: int | None


# -- prompt inputs ----------------------------------------------------------
def campaign_brief(campaign: Campaign) -> CampaignBrief:
    return CampaignBrief(
        product=campaign.product,
        value_proposition=campaign.value_proposition,
        target_audience=campaign.target_audience,
        call_to_action=campaign.call_to_action,
        tone=campaign.tone,
        sender_name=campaign.sender_name,
        sender_company=campaign.sender_company,
    )


def prospect_facts(
    row: CampaignProspectRow, enriched: EnrichedCompany
) -> tuple[ProspectFacts, dict[str, Any], int | None]:
    """Facts for the prompt, provenance for the audit trail, and research ID."""
    _, prospect, candidate, qualification = row
    record = to_qualification_record(qualification)
    website = enriched.website

    # Only evidence that counted and came from the company's own website is
    # quoted; search-result snippets are already present as `description`.
    evidence_items = [
        item
        for factor in record.factors
        if factor.key in _EVIDENCE_FACTORS
        for item in factor.evidence
        if item.counted and item.level == "website"
    ]
    facts = ProspectFacts(
        company_name=candidate.name,
        domain=candidate.domain,
        website_url=candidate.website_url,
        description=candidate.description,
        website_title=website.page_title if website else None,
        website_excerpt=website.extracted_text if website else None,
        matched_signals=record.matched_signals,
        evidence=[item.excerpt for item in evidence_items],
    )
    provenance: dict[str, Any] = {
        "candidate_id": candidate.id,
        "candidate_source_url": candidate.source_url,
        "saved_prospect_id": prospect.id,
        "qualification_id": qualification.id,
        "qualification_rule_version": qualification.rule_version,
        "research_id": website.id if website else None,
        "research_url": (website.final_url or website.requested_url) if website else None,
        "research_fetched_at": website.fetched_at.isoformat() if website else None,
        "evidence": [
            {
                "signal": item.signal,
                "source": item.source,
                "source_url": item.source_url,
                "research_id": item.research_id,
            }
            for item in evidence_items
        ],
    }
    return facts, provenance, website.id if website else None


# -- queries ------------------------------------------------------------------
def _latest_revisions(session: Session, campaign_id: int) -> dict[int, int]:
    stmt = (
        select(OutreachDraft.saved_prospect_id, func.max(OutreachDraft.revision))
        .where(OutreachDraft.campaign_id == campaign_id)
        .group_by(OutreachDraft.saved_prospect_id)
    )
    return {pid: revision for pid, revision in session.execute(stmt).tuples()}


def _draft_at(
    session: Session, campaign_id: int, saved_prospect_id: int, revision: int
) -> OutreachDraft:
    return session.execute(
        select(OutreachDraft).where(
            OutreachDraft.campaign_id == campaign_id,
            OutreachDraft.saved_prospect_id == saved_prospect_id,
            OutreachDraft.revision == revision,
        )
    ).scalar_one()


def _persist(
    session_factory: sessionmaker[Session],
    campaign_id: int,
    plan: _Plan,
    *,
    subject: str,
    body: str,
    provider: str,
    model: str,
    metadata: dict[str, Any],
    regenerate: bool,
    generated_at: datetime,
) -> tuple[OutreachDraft, bool]:
    """Insert one complete draft; return it and whether it was newly created."""
    with session_factory() as session:
        try:
            with session.begin():
                draft_id, created = _insert_revision(
                    session,
                    campaign_id,
                    plan,
                    subject=subject,
                    body=body,
                    provider=provider,
                    model=model,
                    metadata=metadata,
                    regenerate=regenerate,
                    generated_at=generated_at,
                )
        except SQLAlchemyError as exc:
            logger.exception(
                "Draft persistence failed for campaign %s, prospect %s",
                campaign_id,
                plan.saved_prospect_id,
            )
            raise DraftPersistenceError("The draft could not be saved.") from exc
        # Re-read after commit so the returned row is fully loaded regardless
        # of the session factory's expire_on_commit setting.
        draft = session.get(OutreachDraft, draft_id)
        if draft is None:  # pragma: no cover - guarded by the transaction above
            raise DraftPersistenceError("A saved draft could not be read back.")
        return draft, created


def _insert_revision(
    session: Session,
    campaign_id: int,
    plan: _Plan,
    *,
    subject: str,
    body: str,
    provider: str,
    model: str,
    metadata: dict[str, Any],
    regenerate: bool,
    generated_at: datetime,
) -> tuple[int, bool]:
    """Insert the next revision inside the caller's transaction.

    Returns the draft ID to use and whether this call created it.
    """
    latest = session.execute(
        select(func.max(OutreachDraft.revision)).where(
            OutreachDraft.campaign_id == campaign_id,
            OutreachDraft.saved_prospect_id == plan.saved_prospect_id,
        )
    ).scalar_one()
    if latest is not None and not regenerate:
        # A concurrent request drafted this prospect meanwhile; keep theirs.
        return _draft_at(session, campaign_id, plan.saved_prospect_id, latest).id, False
    revision = (latest or 0) + 1
    inserted_id = session.execute(
        pg_insert(OutreachDraft)
        .values(
            campaign_id=campaign_id,
            saved_prospect_id=plan.saved_prospect_id,
            revision=revision,
            state="pending",
            subject=subject,
            body=body,
            prompt_version=plan.prompt.version,
            prompt_sha256=prompt_sha256(plan.prompt),
            provider=provider[:50],
            model=model[:100],
            generation_metadata=metadata,
            provenance=plan.provenance,
            qualification_id=plan.qualification_id,
            research_id=plan.research_id,
            generated_at=generated_at,
        )
        .on_conflict_do_nothing(constraint="uq_outreach_drafts_campaign_prospect_revision")
        .returning(OutreachDraft.id)
    ).scalar_one_or_none()
    if inserted_id is None:
        # Another request inserted this revision first; keep theirs.
        return _draft_at(session, campaign_id, plan.saved_prospect_id, revision).id, False
    return inserted_id, True


def generate_drafts(
    session_factory: sessionmaker[Session],
    generator: DraftGenerator,
    campaign_id: int,
    *,
    saved_prospect_ids: list[int] | None = None,
    regenerate: bool = False,
    now: datetime | None = None,
) -> GenerationResult:
    """Generate drafts for a campaign's prospects (see module docstring)."""
    # 1. Read everything needed, then close the session before any I/O.
    with session_factory() as session:
        campaign = get_campaign(session, campaign_id)
        rows = campaign_prospect_rows(session, campaign_id)
        in_campaign = [link.saved_prospect_id for link, *_ in rows]
        if saved_prospect_ids is not None:
            missing = [pid for pid in saved_prospect_ids if pid not in in_campaign]
            if missing:
                raise ProspectNotInCampaignError(missing)
            wanted = set(saved_prospect_ids)
            rows = [row for row in rows if row[0].saved_prospect_id in wanted]

        latest = _latest_revisions(session, campaign_id)
        existing = {
            row[0].saved_prospect_id: _draft_at(
                session, campaign_id, row[0].saved_prospect_id, latest[row[0].saved_prospect_id]
            )
            for row in rows
            if not regenerate and row[0].saved_prospect_id in latest
        }
        pending_rows = [row for row in rows if row[0].saved_prospect_id not in existing]
        batch = pending_rows[:BATCH_LIMIT]
        remaining = len(pending_rows) - len(batch)

        brief = campaign_brief(campaign)
        plans: dict[int, _Plan] = {}
        for row in batch:
            link, prospect, candidate, qualification = row
            facts, provenance, research_id = prospect_facts(
                row, get_enrichment(session, candidate.id)
            )
            plans[link.saved_prospect_id] = _Plan(
                saved_prospect_id=link.saved_prospect_id,
                prompt=build_prompt(brief, facts),
                provenance=provenance,
                qualification_id=qualification.id,
                research_id=research_id,
            )
        order = [row[0].saved_prospect_id for row in rows]

    # 2-3. Generate (no session open), validate, then persist each draft.
    created: dict[int, tuple[OutreachDraft, bool]] = {}
    for prospect_id, plan in plans.items():
        try:
            draft = generator.generate(plan.prompt)
        except DraftGeneratorError as exc:
            logger.warning(
                "Draft generation failed for campaign %s, prospect %s: %s",
                campaign_id,
                prospect_id,
                exc.code,
            )
            raise DraftGenerationFailed(
                exc,
                failed_saved_prospect_id=prospect_id,
                generated_draft_ids=[d.id for d, new in created.values() if new],
            ) from exc
        created[prospect_id] = _persist(
            session_factory,
            campaign_id,
            plan,
            subject=draft.subject,
            body=draft.body,
            provider=draft.provider,
            model=draft.model,
            metadata=draft.metadata,
            regenerate=regenerate,
            generated_at=now or datetime.now(UTC),
        )

    items: list[GenerationItem] = []
    for prospect_id in order:
        if prospect_id in existing:
            items.append(GenerationItem(prospect_id, "existing", existing[prospect_id]))
        elif prospect_id in created:
            draft_row, is_new = created[prospect_id]
            items.append(
                GenerationItem(prospect_id, "generated" if is_new else "existing", draft_row)
            )
    logger.info(
        "Campaign %s drafts: %s generated, %s existing, %s remaining",
        campaign_id,
        sum(1 for item in items if item.outcome == "generated"),
        sum(1 for item in items if item.outcome == "existing"),
        remaining,
    )
    return GenerationResult(campaign_id=campaign_id, items=items, remaining=remaining)


def _campaign_drafts(campaign_id: int) -> Select[tuple[OutreachDraft]]:
    return (
        select(OutreachDraft)
        .join(
            CampaignProspect,
            (CampaignProspect.campaign_id == OutreachDraft.campaign_id)
            & (CampaignProspect.saved_prospect_id == OutreachDraft.saved_prospect_id),
        )
        .where(OutreachDraft.campaign_id == campaign_id)
    )


def list_drafts(
    session: Session,
    campaign_id: int,
    *,
    limit: int,
    offset: int,
    saved_prospect_id: int | None = None,
) -> tuple[int, list[OutreachDraft]]:
    """Drafts in campaign order, then revision (ties by ID)."""
    get_campaign(session, campaign_id)
    stmt = _campaign_drafts(campaign_id)
    if saved_prospect_id is not None:
        stmt = stmt.where(OutreachDraft.saved_prospect_id == saved_prospect_id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    page = stmt.order_by(
        CampaignProspect.position.asc(), OutreachDraft.revision.asc(), OutreachDraft.id.asc()
    )
    rows = list(session.execute(page.limit(limit).offset(offset)).scalars())
    return total, rows


def get_draft(session: Session, draft_id: int) -> OutreachDraft:
    draft = session.get(OutreachDraft, draft_id)
    if draft is None:
        raise DraftNotFoundError(f"Draft {draft_id} was not found.")
    return draft


def to_draft_record(draft: OutreachDraft) -> DraftRecord:
    return DraftRecord.model_validate(draft, from_attributes=True)
