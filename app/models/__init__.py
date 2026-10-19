"""ORM models.

Import every model here so ``app.models.Base.metadata`` — and therefore
Alembic autogenerate — sees it.
"""

from app.models.base import Base
from app.models.company_candidate import CompanyCandidate

__all__ = ["Base", "CompanyCandidate"]
