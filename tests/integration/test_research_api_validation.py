"""Research API input validation and OpenAPI contract. No database needed.

Every request here is rejected during validation, before the service runs,
so these tests work without PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.providers.website_fake import FakeWebsiteFetcher


@pytest.fixture
def fetcher() -> FakeWebsiteFetcher:
    return FakeWebsiteFetcher()


@pytest.fixture
def client(build_app: Callable[..., FastAPI], fetcher: FakeWebsiteFetcher) -> TestClient:
    application = build_app()
    application.state.website_fetcher = fetcher
    return TestClient(application, raise_server_exceptions=False)


@pytest.mark.parametrize("candidate_id", ["0", "-1", "abc", "1.5", "2147483648"])
@pytest.mark.parametrize(
    ("method", "suffix"),
    [("post", "research"), ("get", "research"), ("get", "enrichment")],
)
def test_invalid_candidate_ids_use_error_envelope(
    client: TestClient,
    fetcher: FakeWebsiteFetcher,
    candidate_id: str,
    method: str,
    suffix: str,
) -> None:
    response = client.request(method.upper(), f"/companies/{candidate_id}/{suffix}")

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"][0]["loc"][:2] == ["path", "candidate_id"]
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "http://169.254.169.254/"},
        {"refresh": True, "website_url": "http://127.0.0.1/"},
        {"refresh": "sometimes"},
        {"refresh": None},
        [],
    ],
)
def test_research_body_rejects_urls_and_bad_options(
    client: TestClient, fetcher: FakeWebsiteFetcher, payload: object
) -> None:
    response = client.post("/companies/1/research", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert fetcher.calls == []


def test_openapi_documents_research_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    research = paths["/companies/{candidate_id}/research"]
    assert set(research) == {"post", "get"}
    assert {"404", "409", "422", "500"} <= set(research["post"]["responses"])
    assert "404" in paths["/companies/{candidate_id}/enrichment"]["get"]["responses"]

    request_schema = schema["components"]["schemas"]["ResearchRequest"]
    assert set(request_schema["properties"]) == {"refresh"}
    assert request_schema["additionalProperties"] is False
    assert not any("url" in path.lower() and "{" not in path for path in paths)
