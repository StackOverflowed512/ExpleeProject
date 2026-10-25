"""Fixtures for integration tests that need PostgreSQL.

These tests run only when ``TEST_DATABASE_URL`` is set to a database that is
safe to modify — a dedicated test database such as ``explee_test``. The
suite never touches the application database configured in ``.env``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
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
                "TRUNCATE email_send_attempts, draft_approvals, outreach_drafts, "
                "campaign_prospects, campaigns, "
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


# -- web interface (Phase 6) -------------------------------------------------------
_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


class UiClient:
    """Browser-like client: keeps cookies and the latest page's CSRF token."""

    def __init__(self, http: AsyncClient, app: FastAPI) -> None:
        self.http = http
        self.app = app
        self.token: str | None = None

    def _remember(self, response: Response) -> None:
        match = _CSRF_META.search(response.text)
        if match:
            self.token = match.group(1)

    async def get(self, path: str, **kwargs: Any) -> Response:
        response = await self.http.get(path, **kwargs)
        self._remember(response)
        return response

    async def post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        csrf: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Response:
        fields = dict(data or {})
        if csrf:
            if self.token is None:
                await self.get("/ui/system")
            fields.setdefault("csrf_token", self.token)
        response = await self.http.post(path, data=fields, headers=headers)
        self._remember(response)
        return response


@pytest.fixture
def ui_factory(
    build_app: Any, db_session_factory: sessionmaker[Session], clean_database: None
) -> Any:
    """Build the full app against the test database with injectable fakes."""

    def _make(
        *,
        search: Any = None,
        fetcher: Any = None,
        generator: Any = None,
        mailer: Any = None,
        **settings: Any,
    ) -> Any:
        """Provider objects are injected; any other keyword is a setting."""
        application: FastAPI = build_app(**settings)
        application.state.session_factory = db_session_factory
        if search is not None:
            application.state.search_provider = search
        if fetcher is not None:
            application.state.website_fetcher = fetcher
        if generator is not None:
            application.state.draft_generator = generator
        if mailer is not None:
            application.state.email_provider = mailer

        class _Ctx:
            async def __aenter__(self) -> UiClient:
                self._lifespan = LifespanManager(application)
                await self._lifespan.__aenter__()
                self._http = AsyncClient(
                    transport=ASGITransport(app=application, raise_app_exceptions=False),
                    base_url="http://test",
                )
                return UiClient(self._http, application)

            async def __aexit__(self, *exc_info: object) -> None:
                await self._http.aclose()
                await self._lifespan.__aexit__(*exc_info)

        return _Ctx()

    return _make
