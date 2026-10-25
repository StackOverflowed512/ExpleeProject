"""API tests for approval, sending, and delivery resolution (PostgreSQL).

Requires ``TEST_DATABASE_URL``; skipped otherwise. The email provider is
always :class:`FakeEmailProvider`; no email is sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import get_session, get_session_factory
from app.schemas.campaigns import CampaignCreate
from app.services.campaigns import create_campaign
from app.services.drafts import generate_drafts
from app.services.providers import build_email_provider
from app.services.providers.drafts_fake import FakeDraftGenerator
from app.services.providers.email import EmailDeliveryUncertainError, EmailTimeoutError
from app.services.providers.email_fake import FakeEmailProvider
from tests.integration.outreach_helpers import make_saved_prospect

RECIPIENT = "ops@acme.com"
BRIEF: dict[str, object] = {
    "name": "Delivery API",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
}


@pytest.fixture
def draft_id(db_session: Session, db_session_factory: sessionmaker[Session]) -> int:
    prospect = make_saved_prospect(db_session)
    campaign = create_campaign(
        db_session, CampaignCreate.model_validate(BRIEF | {"prospect_ids": [prospect.id]})
    )
    return generate_drafts(db_session_factory, FakeDraftGenerator(), campaign.id).items[0].draft.id


@pytest.fixture
def app_factory(
    build_app: Any, db_session_factory: sessionmaker[Session], clean_database: None
) -> Any:
    def _make(provider: Any = None, **settings: Any) -> Any:
        overrides = {"email_sender": "alex@routewise.example"} | settings
        application: FastAPI = build_app(**overrides)
        application.state.email_provider = provider or FakeEmailProvider(
            delivers_email=True
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

    return _make


def _envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    return body["error"]


async def _approve(http: AsyncClient, draft_id: int, **overrides: Any) -> Any:
    return await http.post(
        f"/drafts/{draft_id}/approve",
        json={"recipient_email": RECIPIENT, "revision": 1} | overrides,
    )


async def test_approve_then_read_detail(app_factory: Any, draft_id: int) -> None:
    async with app_factory() as http:
        created = await _approve(http, draft_id, recipient_email="  Ops@ACME.com ")
        repeat = await _approve(http, draft_id, recipient_email="Ops@acme.com")
        detail = await http.get(f"/drafts/{draft_id}")

    assert created.status_code == 201
    body = created.json()
    assert body["created"] is True
    approval = body["draft"]["approval"]
    assert approval["recipient_email"] == "Ops@acme.com"
    assert approval["draft_revision"] == 1
    assert approval["approved_by"] == "local-operator"
    assert len(approval["content_sha256"]) == 64
    assert body["draft"]["state"] == "approved"
    assert repeat.status_code == 200 and repeat.json()["created"] is False
    assert detail.json()["approval"] == approval
    assert detail.json()["send_attempts"] == []


async def test_approval_conflicts_and_not_found(app_factory: Any, draft_id: int) -> None:
    async with app_factory() as http:
        await _approve(http, draft_id)
        other_recipient = await _approve(http, draft_id, recipient_email="ceo@acme.com")
        wrong_revision = await _approve(http, draft_id, revision=3)
        missing = await _approve(http, 98765)
    _envelope(other_recipient, 409, "draft_already_approved")
    _envelope(wrong_revision, 409, "draft_revision_mismatch")
    _envelope(missing, 404, "draft_not_found")


async def test_send_requires_approval(app_factory: Any, draft_id: int) -> None:
    async with app_factory() as http:
        response = await http.post(f"/drafts/{draft_id}/send", json={"mode": "dry_run"})
    _envelope(response, 409, "draft_not_approved")


async def test_dry_run_and_delivery_disabled_by_default(app_factory: Any, draft_id: int) -> None:
    provider = FakeEmailProvider()
    async with app_factory(provider) as http:
        await _approve(http, draft_id)
        dry = await http.post(f"/drafts/{draft_id}/send", json={"mode": "dry_run"})
        real = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})

    assert dry.status_code == 200
    body = dry.json()
    assert body["outcome"] == "dry_run"
    assert body["attempt"]["mode"] == "dry_run"
    assert body["draft"]["state"] == "approved"
    assert len(body["draft"]["send_attempts"]) == 1
    _envelope(real, 409, "delivery_disabled")
    assert provider.attempts == 0


async def test_fake_provider_cannot_mark_a_draft_sent(app_factory: Any, draft_id: int) -> None:
    """EMAIL_PROVIDER=fake + EMAIL_DRY_RUN=false: deliver is refused (PR #10 P1)."""
    configured = build_email_provider(
        Settings(_env_file=None, email_provider="fake", email_dry_run=False)
    )
    assert isinstance(configured, FakeEmailProvider)
    async with app_factory(configured, email_dry_run=False) as http:
        await _approve(http, draft_id)
        real = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
        detail = (await http.get(f"/drafts/{draft_id}")).json()

    _envelope(real, 409, "delivery_disabled")
    assert configured.attempts == 0 and configured.sent == []
    assert detail["state"] == "approved"
    assert detail["send_attempts"] == []


async def test_delivery_when_enabled_and_idempotent_repeat(app_factory: Any, draft_id: int) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with app_factory(provider, email_dry_run=False) as http:
        await _approve(http, draft_id)
        sent = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
        repeat = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})

    assert sent.status_code == 200, sent.text
    assert sent.json()["outcome"] == "sent"
    assert sent.json()["draft"]["state"] == "sent"
    assert repeat.json()["outcome"] == "already_sent"
    assert repeat.json()["attempt"]["id"] == sent.json()["attempt"]["id"]
    assert provider.attempts == 1
    assert provider.sent[0].recipient == RECIPIENT


async def test_definite_failure_envelope_is_retry_safe(app_factory: Any, draft_id: int) -> None:
    timeout = EmailTimeoutError("The mail server did not respond in time.")
    provider = FakeEmailProvider(delivers_email=True, error=timeout)
    async with app_factory(provider, email_dry_run=False) as http:
        await _approve(http, draft_id)
        response = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
        detail = await http.get(f"/drafts/{draft_id}")
    error = _envelope(response, 504, "email_timeout")
    assert error["details"]["retry_safe"] is True
    assert detail.json()["state"] == "approved"
    assert detail.json()["send_attempts"][0]["outcome"] == "failed"


async def test_uncertain_outcome_and_manual_resolution(app_factory: Any, draft_id: int) -> None:
    uncertain = EmailDeliveryUncertainError("maybe delivered")
    provider = FakeEmailProvider(delivers_email=True, error=uncertain)
    async with app_factory(provider, email_dry_run=False) as http:
        await _approve(http, draft_id)
        uncertain = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
        blocked = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
        resolved = await http.post(
            f"/drafts/{draft_id}/resolve-delivery",
            json={"delivered": True, "note": "Found in the server log"},
        )
        not_resolvable = await http.post(
            f"/drafts/{draft_id}/resolve-delivery", json={"delivered": False}
        )

    error = _envelope(uncertain, 502, "email_delivery_uncertain")
    assert error["details"]["retry_safe"] is False
    assert isinstance(error["details"]["attempt_id"], int)
    _envelope(blocked, 409, "delivery_outcome_uncertain")
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["state"] == "sent"
    assert [a["mode"] for a in body["send_attempts"]] == ["real", "manual_resolution"]
    assert body["send_attempts"][1]["note"] == "Found in the server log"
    _envelope(not_resolvable, 409, "delivery_not_resolvable")
    assert provider.attempts == 1


async def test_missing_sender_returns_not_configured(app_factory: Any, draft_id: int) -> None:
    async with app_factory(email_dry_run=False, email_sender=None) as http:
        await _approve(http, draft_id)
        response = await http.post(f"/drafts/{draft_id}/send", json={"mode": "deliver"})
    _envelope(response, 503, "email_not_configured")


async def test_send_body_cannot_override_the_approved_message(
    app_factory: Any, draft_id: int
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with app_factory(provider, email_dry_run=False) as http:
        await _approve(http, draft_id)
        for extra in (
            {"recipient_email": "attacker@evil.example"},
            {"to": "attacker@evil.example"},
            {"subject": "Changed"},
            {"body": "Changed"},
        ):
            response = await http.post(
                f"/drafts/{draft_id}/send", json={"mode": "deliver"} | extra
            )
            _envelope(response, 422, "validation_error")
    assert provider.attempts == 0
