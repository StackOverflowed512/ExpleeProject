"""Integration tests for the application and its HTTP contract.

The app is exercised through an in-process HTTPX client; nothing listens on
a port and no database connection is opened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import SERVICE_NAME, __version__


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTPX client running the full app lifespan in-process."""
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": SERVICE_NAME,
        "environment": "testing",
        "version": __version__,
    }


async def test_health_content_type_is_json(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers["content-type"] == "application/json"


async def test_root_lists_entry_points(client: AsyncClient) -> None:
    response = await client.get("/")

    body = response.json()
    assert body["service"] == SERVICE_NAME
    assert body["health"].endswith("/health")
    assert body["docs"].endswith("/docs")


async def test_openapi_documents_health_response(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    ok_response = schema["paths"]["/health"]["get"]["responses"]["200"]
    assert ok_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/HealthResponse"
    )
    health_schema = schema["components"]["schemas"]["HealthResponse"]
    assert set(health_schema["properties"]) == {
        "status",
        "service",
        "environment",
        "version",
    }


async def test_health_survives_unreachable_database(client: AsyncClient) -> None:
    # The test settings point at a PostgreSQL server that may not exist; the
    # endpoint never attempts a connection, so it must still answer.
    response = await client.get("/health")

    assert response.json()["status"] == "ok"


async def test_unknown_route_returns_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_engine_is_disposed_on_shutdown(
    build_app: Callable[..., FastAPI],
) -> None:
    application = build_app()
    engine = application.state.engine

    with patch.object(engine, "dispose") as dispose:
        async with LifespanManager(application):
            assert not dispose.called

    dispose.assert_called_once()
