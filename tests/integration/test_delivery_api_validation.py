"""Validation and OpenAPI checks for approval and delivery endpoints. No database.

Every request here is rejected during request validation, before any
service code runs, so no provider is ever called.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(build_app: Callable[..., FastAPI]) -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


def _assert_validation_error(response) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"revision": 1},
        {"recipient_email": "ops@acme.com"},
        {"recipient_email": "not-an-email", "revision": 1},
        {"recipient_email": "Alex <ops@acme.com>", "revision": 1},
        {"recipient_email": "ops@acme.com\r\nBcc: x@evil.example", "revision": 1},
        {"recipient_email": "ops@acme.com", "revision": 0},
        {"recipient_email": "ops@acme.com", "revision": 1, "approved_by": "ceo"},
        {"recipient_email": ["ops@acme.com"], "revision": 1},
    ],
)
def test_invalid_approval_bodies(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/drafts/1/approve", json=body))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "bulk"},
        {"mode": "deliver", "recipient_email": "x@evil.example"},
        {"mode": "deliver", "draft_ids": [1, 2]},
        {"mode": "deliver", "subject": "x"},
    ],
)
def test_invalid_send_bodies(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/drafts/1/send", json=body))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"delivered": "maybe"},
        {"delivered": True, "note": "x" * 501},
        {"delivered": True, "x": 1},
    ],
)
def test_invalid_resolution_bodies(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/drafts/1/resolve-delivery", json=body))


@pytest.mark.parametrize(
    "path", ["/drafts/0/approve", "/drafts/abc/send", "/drafts/0/resolve-delivery"]
)
def test_invalid_draft_ids(client: TestClient, path: str) -> None:
    _assert_validation_error(client.post(path, json={}))


def test_openapi_documents_the_delivery_contract(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        "/drafts/{draft_id}/approve",
        "/drafts/{draft_id}/send",
        "/drafts/{draft_id}/resolve-delivery",
    ):
        assert set(paths[path]) == {"post"}
    send = paths["/drafts/{draft_id}/send"]["post"]["responses"]
    assert {"404", "409", "422", "500", "502", "503", "504"} <= set(send)
    request = schema["components"]["schemas"]["SendDraftRequest"]
    assert set(request["properties"]) == {"mode"}
    assert request["additionalProperties"] is False
    detail = schema["components"]["schemas"]["DraftDetail"]
    assert {"approval", "send_attempts"} <= set(detail["properties"])
