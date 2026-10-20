"""Integration tests for the research and enrichment endpoints.

Requires ``TEST_DATABASE_URL``; skipped otherwise. Website fetches use the
deterministic fake provider and never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session
from app.models.company_candidate import CompanyCandidate
from app.services.providers.fake_website import FakeWebsiteFetcher
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetchResult,
)

FIXED_NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
WEBSITE = "https://acme.com/"


def _result(
    *,
    text: str = "Acme builds reliable widgets.",
    title: str | None = "Acme Corp",
    outcome: FetchOutcome = FetchOutcome.SUCCESS,
    failure: FetchFailureCategory | None = None,
    status: int | None = 200,
    final_url: str | None = WEBSITE,
    truncated: bool = False,
) -> WebsiteFetchResult:
    return WebsiteFetchResult(
        requested_url=WEBSITE,
        final_url=final_url,
        fetched_at=FIXED_NOW,
        outcome=outcome,
        failure_category=failure,
        status_code=status,
        content_type="text/html",
        page_title=title,
        text=text,
        truncated=truncated,
    )


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


@pytest.fixture
def make_client(
    build_app: Any,
    db_session_factory: sessionmaker[Session],
    clean_database: None,
) -> Callable[[Any], Any]:
    """Build an app with a fake website fetcher and test-database session."""

    def _make(fetcher: Any) -> Any:
        application: FastAPI = build_app(website_fetcher=fetcher)

        def _session_override() -> Iterator[Session]:
            session = db_session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_session] = _session_override
        return _client_for(application)

    return _make


def _candidate(
    factory: sessionmaker[Session],
    *,
    website_url: str | None = WEBSITE,
    name: str = "Acme Corp",
    domain: str | None = "acme.com",
) -> int:
    with factory() as session:
        candidate = CompanyCandidate(
            name=name,
            website_url=website_url,
            domain=domain,
            description="Widgets.",
            provider="fake",
            source_url=website_url or "https://source.example/acme",
        )
        session.add(candidate)
        session.commit()
        return candidate.id


async def test_research_success_and_retrieval(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(by_url={WEBSITE: _result()})

    async with make_client(fetcher) as client:
        response = await client.post(f"/companies/{candidate_id}/research")
        history = await client.get(f"/companies/{candidate_id}/research")
        enrichment = await client.get(
            f"/companies/{candidate_id}/enrichment"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["company_candidate_id"] == candidate_id
    assert body["requested_url"] == WEBSITE
    assert body["final_url"] == WEBSITE
    assert body["page_title"] == "Acme Corp"
    assert body["page_text"] == "Acme builds reliable widgets."
    assert body["outcome"] == "success"
    assert body["failure_category"] is None
    assert body["created"] is True
    assert "attempt_key" not in body
    assert "content_hash" not in body

    assert history.status_code == 200
    history_body = history.json()
    assert history_body["candidate_id"] == candidate_id
    assert len(history_body["records"]) == 1
    assert history_body["latest_successful"]["id"] == body["id"]
    assert "created" not in history_body["records"][0]

    assert enrichment.status_code == 200
    enrichment_body = enrichment.json()
    assert enrichment_body["status"] == "researched"
    assert enrichment_body["candidate"]["id"] == candidate_id
    assert enrichment_body["candidate"]["source_url"] == WEBSITE
    assert enrichment_body["research"]["page_text"] == body["page_text"]


async def test_repeated_research_is_idempotent(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(by_url={WEBSITE: _result()})

    async with make_client(fetcher) as client:
        first = await client.post(f"/companies/{candidate_id}/research")
        second = await client.post(f"/companies/{candidate_id}/research")
        history = await client.get(f"/companies/{candidate_id}/research")

    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["id"] == first.json()["id"]
    assert len(history.json()["records"]) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/companies/999999/research",
        "/companies/999999/enrichment",
    ],
)
async def test_unknown_candidate_returns_not_found(
    make_client: Any, path: str
) -> None:
    async with make_client(FakeWebsiteFetcher()) as client:
        response = await client.get(path)

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "candidate_not_found"
    assert error["details"] is None


async def test_post_unknown_candidate_returns_not_found(
    make_client: Any,
) -> None:
    async with make_client(FakeWebsiteFetcher()) as client:
        response = await client.post("/companies/999999/research")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "candidate_not_found"


async def test_missing_website_returns_422(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(
        db_session_factory, website_url=None, domain="no-site.example"
    )
    fetcher = FakeWebsiteFetcher()

    async with make_client(fetcher) as client:
        response = await client.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "candidate_website_missing"
    assert fetcher.calls == []


@pytest.mark.parametrize("candidate_id", ["abc", "0", "-1", "1.5"])
async def test_invalid_candidate_id_is_rejected(
    make_client: Any, candidate_id: str
) -> None:
    async with make_client(FakeWebsiteFetcher()) as client:
        response = await client.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_arbitrary_url_in_body_is_rejected_without_fetching(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(by_url={WEBSITE: _result()})

    async with make_client(fetcher) as client:
        response = await client.post(
            f"/companies/{candidate_id}/research",
            json={"url": "http://169.254.169.254/latest/meta-data/"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert fetcher.calls == []


async def test_failed_fetch_is_returned_and_retrievable(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(
        by_url={
            WEBSITE: _result(
                outcome=FetchOutcome.FAILED,
                failure=FetchFailureCategory.HTTP_ERROR,
                text="",
                title=None,
                status=503,
            )
        }
    )

    async with make_client(fetcher) as client:
        response = await client.post(f"/companies/{candidate_id}/research")
        history = await client.get(f"/companies/{candidate_id}/research")
        enrichment = await client.get(
            f"/companies/{candidate_id}/enrichment"
        )

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "failed"
    assert body["failure_category"] == "http_error"
    assert body["http_status"] == 503
    assert body["page_text"] is None
    assert history.json()["latest_successful"] is None
    assert enrichment.json()["status"] == "failed"


async def test_partial_fetch_is_reported(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(
        by_url={
            WEBSITE: _result(
                outcome=FetchOutcome.PARTIAL,
                text="Only a little text.",
                truncated=True,
            )
        }
    )

    async with make_client(fetcher) as client:
        response = await client.post(f"/companies/{candidate_id}/research")
        enrichment = await client.get(
            f"/companies/{candidate_id}/enrichment"
        )

    assert response.json()["outcome"] == "partial"
    assert response.json()["truncated"] is True
    assert enrichment.json()["status"] == "partial"


async def test_failure_after_success_keeps_prior_data_visible(
    make_client: Any, db_session_factory: sessionmaker[Session]
) -> None:
    candidate_id = _candidate(db_session_factory)
    fetcher = FakeWebsiteFetcher(by_url={WEBSITE: _result()})

    async with make_client(fetcher) as client:
        success = await client.post(f"/companies/{candidate_id}/research")
        fetcher.by_url[WEBSITE] = _result(
            outcome=FetchOutcome.FAILED,
            failure=FetchFailureCategory.TIMEOUT,
            text="",
            title=None,
            status=None,
        )
        failure = await client.post(f"/companies/{candidate_id}/research")
        history = await client.get(f"/companies/{candidate_id}/research")
        enrichment = await client.get(
            f"/companies/{candidate_id}/enrichment"
        )

    body = history.json()
    assert failure.json()["outcome"] == "failed"
    assert len(body["records"]) == 2
    assert body["latest_successful"]["id"] == success.json()["id"]
    assert body["latest_successful"]["page_text"] == "Acme builds reliable widgets."
    assert enrichment.json()["status"] == "stale"
    assert enrichment.json()["research"]["id"] == success.json()["id"]
    assert enrichment.json()["latest_attempt"]["id"] == failure.json()["id"]


async def test_openapi_documents_research_endpoints(make_client: Any) -> None:
    async with make_client(FakeWebsiteFetcher()) as client:
        schema = (await client.get("/openapi.json")).json()

    paths = schema["paths"]
    assert "/companies/{candidate_id}/research" in paths
    assert "/companies/{candidate_id}/enrichment" in paths

    post = paths["/companies/{candidate_id}/research"]["post"]
    assert post["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ResearchResponse")
    get = paths["/companies/{candidate_id}/research"]["get"]
    assert get["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ResearchHistoryResponse")
    enrichment = paths["/companies/{candidate_id}/enrichment"]["get"]
    assert enrichment["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/EnrichmentResponse")

    research_schema = schema["components"]["schemas"]["ResearchResponse"]
    assert "attempt_key" not in research_schema["properties"]
    assert "content_hash" not in research_schema["properties"]
