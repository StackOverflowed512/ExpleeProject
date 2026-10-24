"""API tests for qualification and saved prospects, plus the end-to-end flow.

Requires ``TEST_DATABASE_URL``; skipped otherwise. Search and website
fetching use fakes; no external service is contacted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session, get_session_factory
from app.models.company_candidate import CompanyCandidate
from app.schemas.search import SearchResultItem
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.website import FetchOutcome, FetchResult
from app.services.providers.website_fake import FakeWebsiteFetcher

URL = "https://acme.com/"
SAAS = {"criteria": {"target_keywords": ["saas"]}}


@pytest.fixture
def fetcher() -> FakeWebsiteFetcher:
    return FakeWebsiteFetcher(
        results={
            URL: FetchResult(
                requested_url=URL,
                fetched_at=datetime(2026, 10, 24, 8, 0, tzinfo=UTC),
                outcome=FetchOutcome.SUCCESS,
                fetcher="fake",
                final_url=URL,
                http_status=200,
                content_type="text/html",
                page_title="Acme",
                text="Acme is a B2B SaaS platform for logistics teams in Berlin.",
            )
        }
    )


@pytest.fixture
def client(
    build_app: Any,
    db_session_factory: sessionmaker[Session],
    clean_database: None,
    fetcher: FakeWebsiteFetcher,
) -> Any:
    application: FastAPI = build_app()
    application.state.website_fetcher = fetcher
    application.state.search_provider = FakeSearchProvider(
        results=[
            SearchResultItem(
                title="Acme Corp", url=URL, snippet="Logistics software.", provider="fake"
            )
        ]
    )

    def _session_override() -> Iterator[Session]:
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_session] = _session_override
    application.dependency_overrides[get_session_factory] = lambda: db_session_factory

    class _Ctx:
        async def __aenter__(self) -> AsyncClient:
            self._lifespan = LifespanManager(application)
            await self._lifespan.__aenter__()
            self._client = AsyncClient(
                transport=ASGITransport(app=application, raise_app_exceptions=False),
                base_url="http://test",
            )
            return self._client

        async def __aexit__(self, *exc_info: object) -> None:
            await self._client.aclose()
            await self._lifespan.__aexit__(*exc_info)

    return _Ctx()


@pytest.fixture
def make_candidate(
    db_session_factory: sessionmaker[Session], clean_database: None
) -> Any:
    def _make(
        name: str = "Acme Corp",
        domain: str = "acme.com",
        description: str | None = "Acme builds SaaS.",
    ) -> int:
        with db_session_factory() as session:
            candidate = CompanyCandidate(
                name=name,
                website_url=f"https://{domain}/",
                domain=domain,
                description=description,
                provider="fake",
                source_url=f"https://{domain}/",
            )
            session.add(candidate)
            session.commit()
            return candidate.id

    return _make


def _assert_envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    return body["error"]


# -- end-to-end ---------------------------------------------------------------
async def test_discover_research_qualify_save_search_flow(
    client: Any, fetcher: FakeWebsiteFetcher
) -> None:
    async with client as http:
        discovery = await http.post("/discovery/search", json={"query": "logistics saas"})
        candidate_id = discovery.json()["results"][0]["id"]

        research = await http.post(f"/companies/{candidate_id}/research")
        qualify = await http.post(
            f"/companies/{candidate_id}/qualify",
            json={"criteria": {"target_keywords": ["SaaS"], "target_locations": ["Berlin"]}},
        )
        save = await http.post(f"/companies/{candidate_id}/save")
        listing = await http.get("/prospects", params={"q": "acme"})
        detail = await http.get(f"/prospects/{save.json()['prospect']['id']}")

    assert discovery.status_code == 200
    assert research.json()["status"] == "available"
    assert fetcher.calls == [URL]

    assert qualify.status_code == 201
    record = qualify.json()
    assert record["status"] == "qualified"
    assert record["score"] == 100
    assert record["matched_signals"] == ["saas", "berlin"]
    assert record["research_id"] == research.json()["attempt"]["id"]

    assert save.status_code == 201
    assert save.json()["created"] is True
    prospect = save.json()["prospect"]
    assert prospect["candidate_id"] == candidate_id
    assert prospect["qualification"]["id"] == record["id"]

    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == prospect["id"]

    body = detail.json()
    assert body["candidate"]["source_url"] == URL
    assert body["candidate"]["provider"] == "fake"
    assert body["qualification"]["rationale"].startswith("Qualified:")
    assert len(body["qualification"]["factors"]) == 3


# -- qualification endpoints -----------------------------------------------------
async def test_qualify_with_defaults_and_response_shape(
    client: Any, make_candidate: Any
) -> None:
    candidate_id = make_candidate()
    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/qualify")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "candidate_id",
        "research_id",
        "rule_version",
        "criteria",
        "status",
        "score",
        "threshold",
        "evidence_coverage",
        "matched_signals",
        "missing_signals",
        "disqualifying_signals",
        "factors",
        "rationale",
        "qualified_at",
        "created_at",
    }
    assert body["rule_version"] == "qualification-rules/v1"
    assert body["criteria"]["profile"] == "default-b2b-software"
    assert body["criteria"]["target_keywords"] == ["b2b", "platform", "saas", "software"]
    assert body["status"] == "qualified"
    factor = body["factors"][0]
    assert set(factor) == {
        "key",
        "label",
        "weight",
        "result",
        "points",
        "disqualifying",
        "signals_found",
        "signals_not_found",
        "evidence",
        "explanation",
    }


async def test_insufficient_evidence_is_reported_distinctly(
    client: Any, make_candidate: Any
) -> None:
    candidate_id = make_candidate(description=None)
    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/qualify", json=SAAS)
    body = response.json()
    assert body["status"] == "insufficient_evidence"
    assert body["evidence_coverage"] == 0.0
    assert body["factors"][0]["result"] == "unknown"


async def test_qualification_history_is_newest_first(client: Any, make_candidate: Any) -> None:
    candidate_id = make_candidate()
    async with client as http:
        first = await http.post(f"/companies/{candidate_id}/qualify", json=SAAS)
        second = await http.post(
            f"/companies/{candidate_id}/qualify",
            json={"criteria": {"target_keywords": ["saas"], "excluded_keywords": ["saas"]}},
        )
        history = await http.get(f"/companies/{candidate_id}/qualifications")
        limited = await http.get(f"/companies/{candidate_id}/qualifications", params={"limit": 1})

    body = history.json()
    assert body["total"] == 2
    assert [r["id"] for r in body["history"]] == [second.json()["id"], first.json()["id"]]
    assert body["latest"]["id"] == second.json()["id"]
    assert body["latest"]["status"] == "unqualified"
    assert body["latest"]["disqualifying_signals"] == ["saas"]
    assert limited.json()["total"] == 2
    assert len(limited.json()["history"]) == 1


async def test_history_for_unqualified_candidate_is_empty(
    client: Any, make_candidate: Any
) -> None:
    candidate_id = make_candidate()
    async with client as http:
        response = await http.get(f"/companies/{candidate_id}/qualifications")
    assert response.json() == {
        "candidate_id": candidate_id,
        "total": 0,
        "latest": None,
        "history": [],
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/companies/424242/qualify"),
        ("GET", "/companies/424242/qualifications"),
        ("POST", "/companies/424242/save"),
    ],
)
async def test_unknown_candidate_uses_not_found_envelope(
    client: Any, method: str, path: str
) -> None:
    async with client as http:
        response = await http.request(method, path)
    error = _assert_envelope(response, 404, "candidate_not_found")
    assert error["message"] == "Company candidate 424242 was not found."


# -- save endpoint ------------------------------------------------------------------
async def test_save_without_qualification_is_conflict(client: Any, make_candidate: Any) -> None:
    candidate_id = make_candidate()
    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/save")
    _assert_envelope(response, 409, "qualification_required")


async def test_save_below_threshold_is_conflict(client: Any, make_candidate: Any) -> None:
    candidate_id = make_candidate(description=None)
    async with client as http:
        await http.post(f"/companies/{candidate_id}/qualify", json=SAAS)
        response = await http.post(f"/companies/{candidate_id}/save")
        listing = await http.get("/prospects")
    error = _assert_envelope(response, 409, "prospect_not_qualified")
    assert "insufficient_evidence" in error["message"]
    assert listing.json()["total"] == 0


async def test_duplicate_save_returns_200_with_existing(client: Any, make_candidate: Any) -> None:
    candidate_id = make_candidate()
    async with client as http:
        await http.post(f"/companies/{candidate_id}/qualify", json=SAAS)
        first = await http.post(f"/companies/{candidate_id}/save")
        second = await http.post(f"/companies/{candidate_id}/save", json={})
        listing = await http.get("/prospects")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["prospect"] == first.json()["prospect"]
    assert listing.json()["total"] == 1


async def test_save_with_foreign_qualification_is_not_found(
    client: Any, make_candidate: Any
) -> None:
    acme = make_candidate()
    beta = make_candidate(name="Beta", domain="beta.io")
    async with client as http:
        beta_run = await http.post(f"/companies/{beta}/qualify", json=SAAS)
        response = await http.post(
            f"/companies/{acme}/save", json={"qualification_id": beta_run.json()["id"]}
        )
    _assert_envelope(response, 404, "qualification_not_found")


# -- prospect listing and detail ------------------------------------------------------
async def test_prospect_listing_filters_and_pagination(
    client: Any, make_candidate: Any
) -> None:
    ids = [
        make_candidate(name=name, domain=domain)
        for name, domain in [("Alpha", "alpha.io"), ("Beta", "beta.io"), ("Gamma", "gamma.io")]
    ]
    async with client as http:
        for candidate_id in ids:
            await http.post(f"/companies/{candidate_id}/qualify", json=SAAS)
            await http.post(f"/companies/{candidate_id}/save")
        page = await http.get("/prospects", params={"limit": 2, "sort": "name_asc"})
        second_page = await http.get(
            "/prospects", params={"limit": 2, "offset": 2, "sort": "name_asc"}
        )
        by_domain = await http.get("/prospects", params={"domain": "beta.io"})
        by_score = await http.get("/prospects", params={"min_score": 100})

    body = page.json()
    assert body["total"] == 3
    assert (body["limit"], body["offset"], body["sort"]) == (2, 0, "name_asc")
    assert [item["name"] for item in body["items"]] == ["Alpha", "Beta"]
    assert [item["name"] for item in second_page.json()["items"]] == ["Gamma"]
    assert [item["name"] for item in by_domain.json()["items"]] == ["Beta"]
    assert by_score.json()["total"] == 3
    summary = body["items"][0]["qualification"]
    assert set(summary) == {
        "id",
        "status",
        "score",
        "threshold",
        "evidence_coverage",
        "rule_version",
        "profile",
        "qualified_at",
    }


async def test_unknown_prospect_uses_not_found_envelope(client: Any) -> None:
    async with client as http:
        response = await http.get("/prospects/777")
    _assert_envelope(response, 404, "prospect_not_found")
