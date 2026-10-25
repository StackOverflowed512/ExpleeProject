"""API tests for campaigns and draft generation (PostgreSQL).

Requires ``TEST_DATABASE_URL``; skipped otherwise. The draft generator is a
fake (or a keyless Mistral adapter that fails before any request).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session, get_session_factory
from app.services.providers.drafts import (
    DraftGeneratorRateLimitError,
    DraftGeneratorTimeoutError,
    DraftGeneratorUnavailableError,
)
from app.services.providers.drafts_fake import FakeDraftGenerator
from app.services.providers.mistral import MistralDraftGenerator
from tests.integration.outreach_helpers import make_candidate, make_saved_prospect

CAMPAIGN: dict[str, Any] = {
    "name": "Q4 fleet operators",
    "description": "Logistics SaaS companies.",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
    "tone": "friendly",
    "sender_name": "Alex",
    "sender_company": "RouteWise",
}
DRAFT_FIELDS = {
    "id",
    "campaign_id",
    "saved_prospect_id",
    "revision",
    "state",
    "subject",
    "body",
    "prompt_version",
    "prompt_sha256",
    "provider",
    "model",
    "generation_metadata",
    "provenance",
    "qualification_id",
    "research_id",
    "generated_at",
    "created_at",
    "updated_at",
}


@pytest.fixture
def generator() -> FakeDraftGenerator:
    return FakeDraftGenerator()


@pytest.fixture
def app_factory(
    build_app: Any,
    db_session_factory: sessionmaker[Session],
    clean_database: None,
) -> Any:
    def _make(draft_generator: Any) -> Any:
        application: FastAPI = build_app()
        application.state.draft_generator = draft_generator

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

    return _make


@pytest.fixture
def client(app_factory: Any, generator: FakeDraftGenerator) -> Any:
    return app_factory(generator)


@pytest.fixture
def prospect_ids(db_session_factory: sessionmaker[Session], clean_database: None) -> list[int]:
    with db_session_factory() as session:
        return [
            make_saved_prospect(session, name=f"Company {i}", domain=f"company{i}.com").id
            for i in range(2)
        ]


def _assert_envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    return body["error"]


async def _create(http: AsyncClient, prospect_ids: list[int], **overrides: Any) -> dict[str, Any]:
    body = CAMPAIGN | {"prospect_ids": prospect_ids} | overrides
    response = await http.post("/campaigns", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# -- campaigns ----------------------------------------------------------------------
async def test_create_and_read_campaign(client: Any, prospect_ids: list[int]) -> None:
    async with client as http:
        created = await _create(http, list(reversed(prospect_ids)))
        detail = await http.get(f"/campaigns/{created['id']}")

    assert set(created) == {
        "id",
        "name",
        "description",
        "product",
        "value_proposition",
        "target_audience",
        "call_to_action",
        "tone",
        "sender_name",
        "sender_company",
        "created_at",
        "updated_at",
        "prospects",
    }
    assert created["tone"] == "friendly"
    assert [p["prospect"]["id"] for p in created["prospects"]] == list(reversed(prospect_ids))
    assert [p["position"] for p in created["prospects"]] == [0, 1]
    assert created["prospects"][0]["prospect"]["qualification"]["status"] == "qualified"
    assert detail.json() == created


async def test_campaign_with_unknown_prospects_is_rejected(
    client: Any, prospect_ids: list[int]
) -> None:
    async with client as http:
        response = await http.post(
            "/campaigns", json=CAMPAIGN | {"prospect_ids": [prospect_ids[0], 9999]}
        )
        listing = await http.get("/campaigns")
    error = _assert_envelope(response, 422, "invalid_prospect_ids")
    assert error["details"] == {"missing_prospect_ids": [9999]}
    assert listing.json()["total"] == 0


async def test_unsaved_candidate_ids_are_rejected(
    client: Any, db_session_factory: sessionmaker[Session], clean_database: None
) -> None:
    with db_session_factory() as session:
        candidate_id = make_candidate(session, domain="lead.com").id
    async with client as http:
        response = await http.post("/campaigns", json=CAMPAIGN | {"prospect_ids": [candidate_id]})
    _assert_envelope(response, 422, "invalid_prospect_ids")


async def test_duplicate_prospect_ids_are_rejected(client: Any, prospect_ids: list[int]) -> None:
    async with client as http:
        response = await http.post(
            "/campaigns", json=CAMPAIGN | {"prospect_ids": [prospect_ids[0], prospect_ids[0]]}
        )
    _assert_envelope(response, 422, "validation_error")


async def test_campaign_listing_pagination(client: Any, prospect_ids: list[int]) -> None:
    async with client as http:
        first = await _create(http, prospect_ids[:1], name="First")
        second = await _create(http, prospect_ids, name="Second")
        page = await http.get("/campaigns", params={"limit": 1})
        rest = await http.get("/campaigns", params={"limit": 1, "offset": 1})

    body = page.json()
    assert (body["total"], body["limit"], body["offset"]) == (2, 1, 0)
    assert [c["id"] for c in body["items"]] == [second["id"]]
    assert body["items"][0]["prospect_count"] == 2
    assert [c["id"] for c in rest.json()["items"]] == [first["id"]]


async def test_unknown_campaign_envelopes(client: Any) -> None:
    async with client as http:
        for method, path in (
            ("GET", "/campaigns/4242"),
            ("POST", "/campaigns/4242/drafts"),
            ("GET", "/campaigns/4242/drafts"),
        ):
            response = await http.request(method, path)
            error = _assert_envelope(response, 404, "campaign_not_found")
            assert error["message"] == "Campaign 4242 was not found."


# -- drafts --------------------------------------------------------------------------
async def test_generate_list_and_read_drafts(
    client: Any, prospect_ids: list[int], generator: FakeDraftGenerator
) -> None:
    async with client as http:
        campaign = await _create(http, prospect_ids)
        generated = await http.post(f"/campaigns/{campaign['id']}/drafts")
        listing = await http.get(f"/campaigns/{campaign['id']}/drafts")
        one = await http.get(f"/drafts/{generated.json()['results'][0]['draft']['id']}")

    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert set(body) == {"campaign_id", "batch_limit", "results", "remaining"}
    assert body["remaining"] == 0
    assert [r["outcome"] for r in body["results"]] == ["generated", "generated"]
    draft = body["results"][0]["draft"]
    assert set(draft) == DRAFT_FIELDS
    assert draft["state"] == "pending"
    assert draft["prompt_version"] == "outreach-draft/v1"
    assert (draft["provider"], draft["model"]) == ("fake", "fake-drafts-1")
    assert draft["provenance"]["saved_prospect_id"] == prospect_ids[0]
    assert len(generator.prompts) == 2

    assert listing.json()["total"] == 2
    assert [d["saved_prospect_id"] for d in listing.json()["items"]] == prospect_ids
    # GET /drafts/{id} adds the Phase 5 approval and delivery audit trail.
    detail = one.json()
    assert {key: detail[key] for key in draft} == draft
    assert detail["approval"] is None
    assert detail["send_attempts"] == []


async def test_repeat_and_regenerate(client: Any, prospect_ids: list[int]) -> None:
    async with client as http:
        campaign = await _create(http, prospect_ids[:1])
        url = f"/campaigns/{campaign['id']}/drafts"
        first = await http.post(url)
        repeat = await http.post(url, json={})
        regenerated = await http.post(url, json={"regenerate": True})
        listing = await http.get(url)

    assert repeat.json()["results"][0]["outcome"] == "existing"
    assert repeat.json()["results"][0]["draft"] == first.json()["results"][0]["draft"]
    assert regenerated.json()["results"][0]["outcome"] == "generated"
    assert regenerated.json()["results"][0]["draft"]["revision"] == 2
    assert [d["revision"] for d in listing.json()["items"]] == [1, 2]


async def test_subset_must_belong_to_campaign(client: Any, prospect_ids: list[int]) -> None:
    async with client as http:
        campaign = await _create(http, prospect_ids[:1])
        response = await http.post(
            f"/campaigns/{campaign['id']}/drafts",
            json={"saved_prospect_ids": [prospect_ids[1]]},
        )
    error = _assert_envelope(response, 422, "prospect_not_in_campaign")
    assert error["details"] == {"missing_saved_prospect_ids": [prospect_ids[1]]}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DraftGeneratorTimeoutError("The draft generator timed out."), 504, "provider_timeout"),
        (DraftGeneratorRateLimitError("rate limited"), 503, "provider_rate_limited"),
        (DraftGeneratorUnavailableError("unavailable"), 502, "provider_unavailable"),
    ],
)
async def test_provider_failures_use_safe_envelopes(
    app_factory: Any, prospect_ids: list[int], error: Exception, status: int, code: str
) -> None:
    failing = FakeDraftGenerator(error=error, fail_on_call=2)  # type: ignore[arg-type]
    async with app_factory(failing) as http:
        campaign = await _create(http, prospect_ids)
        response = await http.post(f"/campaigns/{campaign['id']}/drafts")
        listing = await http.get(f"/campaigns/{campaign['id']}/drafts")

    body = _assert_envelope(response, status, code)
    assert body["details"]["failed_saved_prospect_id"] == prospect_ids[1]
    saved = listing.json()["items"]
    assert body["details"]["generated_draft_ids"] == [saved[0]["id"]]
    assert len(saved) == 1


async def test_missing_mistral_key_returns_configuration_error(
    app_factory: Any, prospect_ids: list[int], db_session_factory: sessionmaker[Session]
) -> None:
    keyless = MistralDraftGenerator(None, model="mistral-small-latest")
    async with app_factory(keyless) as http:
        campaign = await _create(http, prospect_ids)
        response = await http.post(f"/campaigns/{campaign['id']}/drafts")
    error = _assert_envelope(response, 503, "provider_not_configured")
    assert "MISTRAL_API_KEY" in error["message"]
    assert error["details"]["generated_draft_ids"] == []
    with db_session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM outreach_drafts")).scalar_one() == 0


async def test_malformed_output_returns_invalid_output_error(
    app_factory: Any, prospect_ids: list[int]
) -> None:
    async with app_factory(FakeDraftGenerator(output=lambda prompt: '{"subject": ""}')) as http:
        campaign = await _create(http, prospect_ids[:1])
        response = await http.post(f"/campaigns/{campaign['id']}/drafts")
        listing = await http.get(f"/campaigns/{campaign['id']}/drafts")
    _assert_envelope(response, 502, "provider_invalid_output")
    assert listing.json()["total"] == 0


async def test_unknown_draft(client: Any) -> None:
    async with client as http:
        response = await http.get("/drafts/987")
    _assert_envelope(response, 404, "draft_not_found")
