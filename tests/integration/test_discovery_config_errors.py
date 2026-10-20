"""Integration tests for discovery configuration without external services.

These tests do not require PostgreSQL or a Tavily key: the provider rejects
the request before any database or network access.
"""

from __future__ import annotations

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.services.providers.fake import FakeSearchProvider
from tests.conftest import TEST_DATABASE_URL


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="testing",
        database_url=TEST_DATABASE_URL,
        log_level="WARNING",
        search_provider="tavily",
        tavily_api_key=None,
    )


async def test_app_starts_and_health_works_without_a_tavily_key() -> None:
    application = create_app(_settings())

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_discovery_without_a_tavily_key_returns_safe_error() -> None:
    application = create_app(_settings())

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                "/discovery/search", json={"query": "b2b saas berlin"}
            )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "provider_not_configured"
    assert "TAVILY_API_KEY" in error["message"]
    assert "tvly-" not in error["message"]


def test_fake_provider_can_be_injected_without_credentials() -> None:
    settings = _settings()
    provider = FakeSearchProvider()
    application = create_app(settings, search_provider=provider)

    assert application.state.search_provider is provider
    assert application.state.search_provider.name == "fake"
    application.state.engine.dispose()
