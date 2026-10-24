"""Integration tests for the research and enrichment endpoints.

Requires ``TEST_DATABASE_URL``; skipped otherwise. The website fetcher is
always :class:`FakeWebsiteFetcher` — no network access happens.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session
from app.models.company_candidate import CompanyCandidate
from app.services import research as research_service
from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult
from app.services.providers.website_fake import FakeWebsiteFetcher

URL = "https://acme.com/"
FETCHED_AT = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)

ATTEMPT_FIELDS = {
    "id",
    "candidate_id",
    "outcome",
    "failure_category",
    "requested_url",
    "final_url",
    "http_status",
    "content_type",
    "page_title",
    "content_truncated",
    "fetcher",
    "fetched_at",
    "created_at",
    "updated_at",
}


def _ok(**overrides: object) -> FetchResult:
    values: dict[str, object] = {
        "requested_url": URL,
        "fetched_at": FETCHED_AT,
        "outcome": FetchOutcome.SUCCESS,
        "fetcher": "fake",
        "final_url": "https://acme.com/en",
        "http_status": 200,
        "content_type": "text/html",
        "page_title": "Acme Widgets",
        "text": "Widgets that ship",
    }
    values.update(overrides)
    return FetchResult(**values)  # type: ignore[arg-type]


def _failed(failure: FetchFailure = FetchFailure.CONNECTION_FAILED) -> FetchResult:
    return FetchResult(
        requested_url=URL,
        fetched_at=FETCHED_AT,
        outcome=FetchOutcome.FAILED,
        fetcher="fake",
        failure=failure,
    )


@pytest.fixture
def make_candidate(
    db_session_factory: sessionmaker[Session], clean_database: None
) -> Callable[..., int]:
    def _make(website_url: str | None = URL, domain: str | None = "acme.com") -> int:
        with db_session_factory() as session:
            candidate = CompanyCandidate(
                name="Acme Corp",
                website_url=website_url,
                domain=domain,
                description="Acme builds widgets.",
                provider="tavily",
                source_url="https://acme.com/",
            )
            session.add(candidate)
            session.commit()
            return candidate.id

    return _make


@pytest.fixture
def fetcher() -> FakeWebsiteFetcher:
    return FakeWebsiteFetcher(results={URL: _ok()})


@pytest.fixture
def client(
    build_app: Any,
    db_session_factory: sessionmaker[Session],
    clean_database: None,
    fetcher: FakeWebsiteFetcher,
) -> Any:
    # Fixture results carry a fixed FETCHED_AT; a one-year freshness window
    # keeps reuse deterministic relative to the real clock.
    application: FastAPI = build_app(research_freshness_hours=8760.0)
    application.state.website_fetcher = fetcher

    def _session_override() -> Iterator[Session]:
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_session] = _session_override

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


async def test_research_request_fetches_and_persists(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"candidate_id", "status", "reused", "attempt", "latest_usable"}
    assert body["candidate_id"] == candidate_id
    assert body["status"] == "available"
    assert body["reused"] is False
    assert set(body["attempt"]) == ATTEMPT_FIELDS
    assert body["attempt"]["outcome"] == "success"
    assert body["attempt"]["requested_url"] == URL
    assert body["attempt"]["final_url"] == "https://acme.com/en"
    assert body["latest_usable"]["extracted_text"] == "Widgets that ship"
    assert datetime.fromisoformat(body["latest_usable"]["fetched_at"]) == FETCHED_AT
    assert fetcher.calls == [URL]


async def test_second_request_reuses_and_refresh_fetches(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()
    async with client as http:
        first = await http.post(f"/companies/{candidate_id}/research", json={})
        second = await http.post(f"/companies/{candidate_id}/research")
        third = await http.post(
            f"/companies/{candidate_id}/research", json={"refresh": True}
        )

    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert second.json()["attempt"]["id"] == first.json()["attempt"]["id"]
    assert third.json()["reused"] is False
    assert third.json()["attempt"]["id"] != first.json()["attempt"]["id"]
    assert len(fetcher.calls) == 2


async def test_fetch_failure_is_reported_not_raised(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()
    fetcher.results[URL] = _failed(FetchFailure.TIMEOUT)

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["attempt"]["outcome"] == "failed"
    assert body["attempt"]["failure_category"] == "timeout"
    assert body["latest_usable"] is None


async def test_failure_after_success_keeps_latest_usable(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()

    async with client as http:
        first = await http.post(f"/companies/{candidate_id}/research")
        fetcher.results[URL] = _failed(FetchFailure.HTTP_ERROR)
        second = await http.post(
            f"/companies/{candidate_id}/research", json={"refresh": True}
        )
        stored = await http.get(f"/companies/{candidate_id}/research")

    assert second.json()["attempt"]["outcome"] == "failed"
    assert second.json()["status"] == "available"
    assert second.json()["latest_usable"]["id"] == first.json()["attempt"]["id"]

    history = stored.json()
    assert history["status"] == "available"
    assert [a["outcome"] for a in history["attempts"]] == ["failed", "success"]
    assert all(set(a) == ATTEMPT_FIELDS for a in history["attempts"])
    assert history["latest_usable"]["extracted_text"] == "Widgets that ship"


async def test_partial_research_is_distinct(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()
    fetcher.results[URL] = _ok(outcome=FetchOutcome.PARTIAL, content_truncated=True)

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.json()["status"] == "partial"
    assert response.json()["attempt"]["content_truncated"] is True


async def test_get_research_before_any_attempt(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()

    async with client as http:
        response = await http.get(f"/companies/{candidate_id}/research")

    assert response.status_code == 200
    assert response.json() == {
        "candidate_id": candidate_id,
        "status": "not_researched",
        "latest_usable": None,
        "attempts": [],
    }
    assert fetcher.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/companies/987654/research"),
        ("GET", "/companies/987654/research"),
        ("GET", "/companies/987654/enrichment"),
    ],
)
async def test_unknown_candidate_returns_not_found_envelope(
    client: Any, method: str, path: str
) -> None:
    async with client as http:
        response = await http.request(method, path)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "candidate_not_found",
            "message": "Company candidate 987654 was not found.",
            "details": None,
        }
    }


async def test_candidate_without_website_returns_conflict(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate(website_url=None, domain=None)

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "candidate_website_missing"
    assert fetcher.calls == []


async def test_blocked_stored_url_is_recorded_without_fetch(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate(website_url="http://169.254.169.254/latest/", domain=None)

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 200
    assert response.json()["attempt"]["failure_category"] == "blocked_destination"
    assert fetcher.calls == []


async def test_persistence_failure_uses_safe_envelope(
    client: Any,
    make_candidate: Callable[..., int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id = make_candidate()

    def broken_prune(session: Session, candidate_id: int) -> None:
        raise OperationalError("DELETE FROM company_research", {}, Exception("secret dsn"))

    monkeypatch.setattr(research_service, "_prune_history", broken_prune)

    async with client as http:
        response = await http.post(f"/companies/{candidate_id}/research")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "research_persistence_failed"
    assert "secret" not in error["message"]
    assert "DELETE" not in error["message"]


async def test_enrichment_combines_discovery_and_research(
    client: Any, make_candidate: Callable[..., int], fetcher: FakeWebsiteFetcher
) -> None:
    candidate_id = make_candidate()

    async with client as http:
        before = await http.get(f"/companies/{candidate_id}/enrichment")
        await http.post(f"/companies/{candidate_id}/research")
        first = await http.get(f"/companies/{candidate_id}/enrichment")
        second = await http.get(f"/companies/{candidate_id}/enrichment")

    assert before.status_code == 200
    assert before.json()["research_status"] == "not_researched"
    assert before.json()["website"] is None

    body = first.json()
    assert first.json() == second.json()
    assert len(fetcher.calls) == 1
    assert body["candidate_id"] == candidate_id
    assert body["name"] == "Acme Corp"
    assert body["description"] == "Acme builds widgets."
    assert body["discovery"]["provider"] == "tavily"
    assert body["discovery"]["source_url"] == "https://acme.com/"
    assert body["research_status"] == "available"
    assert body["website"]["extracted_text"] == "Widgets that ship"
    assert body["website"]["final_url"] == "https://acme.com/en"
    assert body["last_attempt"]["outcome"] == "success"
