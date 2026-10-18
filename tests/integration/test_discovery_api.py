"""Integration tests for ``POST /discovery/search``.

Requires ``TEST_DATABASE_URL``; skipped otherwise. The search provider is
always a fake — no external API is called.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session
from app.schemas.search import SearchResultItem
from app.services.providers.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.services.providers.fake import FakeSearchProvider


class RaisingProvider:
    """Provider stub that always raises a given error."""

    name = "fake"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def search(self, query: str, limit: int) -> list[SearchResultItem]:
        raise self._error


def _item(url: str, title: str = "Acme Corp") -> SearchResultItem:
    return SearchResultItem(
        title=title, url=url, snippet="Widgets.", provider="fake"
    )


@pytest.fixture
def make_client(
    build_app: Any,
    db_session_factory: sessionmaker[Session],
    clean_database: None,
) -> Callable[[Any], Any]:
    """Build an app with a fake provider and a test-database session."""

    def _make(provider: Any) -> Any:
        application: FastAPI = build_app()
        application.state.search_provider = provider

        def _session_override() -> Iterator[Session]:
            session = db_session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_session] = _session_override
        return _client_for(application)

    return _make


def _client_for(application: FastAPI) -> Any:
    class _Ctx:
        async def __aenter__(self) -> AsyncClient:
            self._lifespan = LifespanManager(application)
            await self._lifespan.__aenter__()
            self._client = AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
            )
            return self._client

        async def __aexit__(self, *exc_info: object) -> None:
            await self._client.aclose()
            await self._lifespan.__aexit__(*exc_info)

    return _Ctx()


async def test_valid_request_returns_persisted_candidates(make_client: Any) -> None:
    provider = FakeSearchProvider(
        results=[_item("https://acme.com"), _item("https://beta.io", "Beta")]
    )

    async with make_client(provider) as client:
        response = await client.post(
            "/discovery/search", json={"query": "b2b saas berlin", "limit": 5}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "b2b saas berlin"
    assert body["provider"] == "fake"
    assert body["requested_limit"] == 5
    assert body["counts"] == {
        "returned": 2,
        "created": 2,
        "updated": 0,
        "skipped": 0,
    }
    first = body["results"][0]
    assert set(first) == {
        "id",
        "name",
        "website_url",
        "domain",
        "description",
        "provider",
        "provider_result_id",
        "source_url",
        "discovered_at",
        "updated_at",
    }


@pytest.mark.parametrize("query", ["", " ", "a"])
async def test_invalid_queries_use_error_envelope(
    make_client: Any, query: str
) -> None:
    async with make_client(FakeSearchProvider()) as client:
        response = await client.post("/discovery/search", json={"query": query})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("limit", [0, 11, -3])
async def test_invalid_limits_are_rejected(make_client: Any, limit: int) -> None:
    async with make_client(FakeSearchProvider()) as client:
        response = await client.post(
            "/discovery/search", json={"query": "valid query", "limit": limit}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_overlong_query_is_rejected(make_client: Any) -> None:
    async with make_client(FakeSearchProvider()) as client:
        response = await client.post(
            "/discovery/search", json={"query": "x" * 301}
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ProviderAuthenticationError("auth failed"), 502, "provider_authentication_failed"),
        (ProviderRateLimitError("rate limited"), 503, "provider_rate_limited"),
        (ProviderTimeoutError("timed out"), 504, "provider_timeout"),
    ],
)
async def test_provider_failures_map_to_error_envelope(
    make_client: Any,
    error: Exception,
    status: int,
    code: str,
) -> None:
    async with make_client(RaisingProvider(error)) as client:
        response = await client.post("/discovery/search", json={"query": "anything"})

    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert "key" not in body["message"].lower()


async def test_duplicate_search_reports_update_not_create(make_client: Any) -> None:
    provider = FakeSearchProvider(results=[_item("https://acme.com")])

    async with make_client(provider) as client:
        first = await client.post("/discovery/search", json={"query": "acme"})
        second = await client.post("/discovery/search", json={"query": "acme again"})

    assert first.json()["counts"]["created"] == 1
    assert second.json()["counts"]["created"] == 0
    assert second.json()["counts"]["returned"] == 1
