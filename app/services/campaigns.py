"""Outreach campaigns built from saved prospects.

A campaign can only contain prospects that exist in ``saved_prospects``:
every requested ID is checked before anything is written, and the whole
campaign (row plus prospect links) is committed in one transaction. The
composite primary key on ``campaign_prospects`` rejects duplicate links at
the database level as well.
"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignProspect
from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.saved_prospect import SavedProspect
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignDetail,
    CampaignProspectItem,
    CampaignSummary,
    CampaignTone,
)
from app.services.prospects import to_prospect_summary

logger = logging.getLogger(__name__)

CampaignProspectRow = tuple[
    CampaignProspect, SavedProspect, CompanyCandidate, CompanyQualification
]


class CampaignNotFoundError(Exception):
    """No campaign exists with the requested ID."""


class InvalidProspectIdsError(Exception):
    """Some requested IDs are not saved prospects."""

    def __init__(self, missing: list[int]) -> None:
        super().__init__(
            "These IDs are not saved prospects: " + ", ".join(str(pid) for pid in missing)
        )
        self.missing = missing


class CampaignPersistenceError(Exception):
    """A campaign could not be saved."""


def create_campaign(session: Session, payload: CampaignCreate) -> Campaign:
    """Create a campaign with its saved prospects, in request order."""
    ids = payload.prospect_ids
    found = set(
        session.execute(select(SavedProspect.id).where(SavedProspect.id.in_(ids))).scalars()
    )
    missing = [pid for pid in ids if pid not in found]
    if missing:
        raise InvalidProspectIdsError(missing)

    campaign = Campaign(
        name=payload.name,
        description=payload.description,
        product=payload.product,
        value_proposition=payload.value_proposition,
        target_audience=payload.target_audience,
        call_to_action=payload.call_to_action,
        tone=payload.tone,
        sender_name=payload.sender_name,
        sender_company=payload.sender_company,
    )
    try:
        session.add(campaign)
        session.flush()
        session.add_all(
            CampaignProspect(campaign_id=campaign.id, saved_prospect_id=pid, position=index)
            for index, pid in enumerate(ids)
        )
        session.commit()
        session.refresh(campaign)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("Campaign persistence failed")
        raise CampaignPersistenceError("The campaign could not be saved.") from exc

    logger.info("Created campaign %s with %s prospects", campaign.id, len(ids))
    return campaign


def get_campaign(session: Session, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFoundError(f"Campaign {campaign_id} was not found.")
    return campaign


def campaign_prospect_rows(session: Session, campaign_id: int) -> list[CampaignProspectRow]:
    """The campaign's prospects with candidate and qualification, by position."""
    stmt = (
        select(CampaignProspect, SavedProspect, CompanyCandidate, CompanyQualification)
        .join(SavedProspect, CampaignProspect.saved_prospect_id == SavedProspect.id)
        .join(CompanyCandidate, SavedProspect.candidate_id == CompanyCandidate.id)
        .join(CompanyQualification, SavedProspect.qualification_id == CompanyQualification.id)
        .where(CampaignProspect.campaign_id == campaign_id)
        .order_by(CampaignProspect.position.asc())
    )
    return [(cp, p, c, q) for cp, p, c, q in session.execute(stmt).tuples()]


def list_campaigns(
    session: Session, *, limit: int, offset: int
) -> tuple[int, list[tuple[Campaign, int]]]:
    """Return the campaign count and one page, newest first (ties by ID)."""
    total = session.execute(select(func.count()).select_from(Campaign)).scalar_one()
    counts = (
        select(
            CampaignProspect.campaign_id,
            func.count().label("prospect_count"),
        )
        .group_by(CampaignProspect.campaign_id)
        .subquery()
    )
    stmt = (
        select(Campaign, func.coalesce(counts.c.prospect_count, 0))
        .outerjoin(counts, counts.c.campaign_id == Campaign.id)
        .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return total, [(campaign, count) for campaign, count in session.execute(stmt).tuples()]


# -- serialization ------------------------------------------------------
def to_campaign_summary(campaign: Campaign, prospect_count: int) -> CampaignSummary:
    return CampaignSummary(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        product=campaign.product,
        tone=cast(CampaignTone, campaign.tone),
        prospect_count=prospect_count,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
    )


def to_campaign_detail(campaign: Campaign, rows: list[CampaignProspectRow]) -> CampaignDetail:
    return CampaignDetail(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        product=campaign.product,
        value_proposition=campaign.value_proposition,
        target_audience=campaign.target_audience,
        call_to_action=campaign.call_to_action,
        tone=cast(CampaignTone, campaign.tone),
        sender_name=campaign.sender_name,
        sender_company=campaign.sender_company,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        prospects=[
            CampaignProspectItem(
                position=link.position,
                added_at=link.added_at,
                prospect=to_prospect_summary(prospect, candidate, qualification),
            )
            for link, prospect, candidate, qualification in rows
        ],
    )
