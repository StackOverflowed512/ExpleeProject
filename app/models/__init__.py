"""ORM models.

Import every model here so ``app.models.Base.metadata`` — and therefore
Alembic autogenerate — sees it.
"""

from app.models.base import Base
from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.company_research import CompanyResearch
from app.models.saved_prospect import SavedProspect

__all__ = [
    "Base",
    "CompanyCandidate",
    "CompanyQualification",
    "CompanyResearch",
    "SavedProspect",
]
