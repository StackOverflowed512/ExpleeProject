"""Fixtures for integration tests that need PostgreSQL.

These tests run only when ``TEST_DATABASE_URL`` is set to a database that is
safe to modify — a dedicated test database such as ``explee_test``. The
suite never touches the application database configured in ``.env``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"


def _test_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set; skipping DB test")
    return url


def _unreachable_reason(exc: Exception) -> str:
    """First line of the driver error, which never contains the password."""
    message = str(exc).splitlines()[0]
    return f"Test database is unreachable: {message[:200]}"


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Engine connected to the dedicated test database."""
    engine = create_engine(_test_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        engine.dispose()
        pytest.skip(_unreachable_reason(exc))

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture
def clean_database(db_engine: Engine) -> None:
    """Truncate all application tables before every test that uses the database.

    Applied by both session-based and API-based fixtures so each test starts
    from empty tables regardless of how it obtains its session.
    """
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE outreach_drafts, campaign_prospects, campaigns, "
                "saved_prospects, company_qualifications, company_research, "
                "company_candidates RESTART IDENTITY"
            )
        )


@pytest.fixture
def db_session(
    db_session_factory: sessionmaker[Session],
    clean_database: None,
) -> Iterator[Session]:
    """A session against empty application tables."""
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()
