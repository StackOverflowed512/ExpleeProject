"""ORM models.

Phase 0 defines only the declarative base; feature models arrive with their
own phases. New models must be imported here so Alembic autogenerate sees
them through ``app.models.Base.metadata``.
"""

from app.models.base import Base

__all__ = ["Base"]
