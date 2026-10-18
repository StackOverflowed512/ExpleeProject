"""SQLAlchemy engine and session management.

Each FastAPI application instance owns one engine and one session factory,
stored on ``app.state``. Every request receives its own session through
:func:`get_session`; sessions are never shared between requests.

Neither importing this module nor building an engine opens a database
connection. Connections are opened lazily on first use.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def build_engine(settings: Settings) -> Engine:
    """Create the SQLAlchemy engine for the configured PostgreSQL database."""
    return create_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the session factory bound to ``engine``."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency that yields a request-scoped database session.

    The session is closed when the request finishes. Read-only handlers may
    use it as-is; handlers that modify data are responsible for committing
    inside their service layer.
    """
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
