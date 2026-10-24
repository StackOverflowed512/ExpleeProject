"""ORM models.

Import every model here so ``app.models.Base.metadata`` — and therefore
Alembic autogenerate — sees it.
"""

from app.models.base import Base
from app.models.campaign import Campaign, CampaignProspect
from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.company_research import CompanyResearch
from app.models.outreach_draft import OutreachDraft
from app.models.saved_prospect import SavedProspect

__all__ = [
    "Base",
    "Campaign",
    "CampaignProspect",
    "CompanyCandidate",
    "CompanyQualification",
    "CompanyResearch",
    "OutreachDraft",
    "SavedProspect",
]
