"""Validation, OpenAPI, and no-sending checks for outreach endpoints.

No database needed: every request here is rejected during validation.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT

CAMPAIGN: dict[str, object] = {
    "name": "Q4",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Logistics companies",
    "call_to_action": "Open to a short call?",
    "prospect_ids": [1],
}


@pytest.fixture
def client(build_app: Callable[..., FastAPI]) -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


def _assert_validation_error(response) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "overrides",
    [
        {"prospect_ids": []},
        {"prospect_ids": [1, 1]},
        {"prospect_ids": [0]},
        {"call_to_action": "Mail me: a@b.com"},
        {"value_proposition": "Hello [Name], we cut planning time a lot."},
        {"tone": "pushy"},
        {"unknown": "field"},
    ],
)
def test_invalid_campaign_bodies(client: TestClient, overrides: dict[str, object]) -> None:
    _assert_validation_error(client.post("/campaigns", json=CAMPAIGN | overrides))


@pytest.mark.parametrize(
    "path",
    ["/campaigns/0", "/campaigns/abc", "/campaigns/0/drafts", "/drafts/0", "/drafts/2147483648"],
)
def test_invalid_ids(client: TestClient, path: str) -> None:
    _assert_validation_error(client.get(path))


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_invalid_pagination(client: TestClient, params: dict[str, object]) -> None:
    _assert_validation_error(client.get("/campaigns", params=params))
    _assert_validation_error(client.get("/campaigns/1/drafts", params=params))


@pytest.mark.parametrize(
    "body",
    [
        {"saved_prospect_ids": []},
        {"saved_prospect_ids": [2, 2]},
        {"regenerate": "maybe"},  # "yes"/"no" are valid lax booleans
        {"send": True},
        {"approve": True},
    ],
)
def test_invalid_generation_bodies(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/campaigns/1/drafts", json=body))


def test_openapi_documents_outreach_contract(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert set(paths["/campaigns"]) == {"post", "get"}
    assert set(paths["/campaigns/{campaign_id}"]) == {"get"}
    assert set(paths["/campaigns/{campaign_id}/drafts"]) == {"post", "get"}
    assert set(paths["/drafts/{draft_id}"]) == {"get"}
    generate = paths["/campaigns/{campaign_id}/drafts"]["post"]["responses"]
    assert {"404", "422", "500", "502", "503", "504"} <= set(generate)
    draft = schema["components"]["schemas"]["DraftRecord"]
    assert draft["properties"]["state"]["const"] == "pending" or draft["properties"]["state"].get(
        "enum"
    ) == ["pending"]


def test_no_endpoint_can_send_or_approve(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path, operations in paths.items():
        lowered = path.lower()
        assert not any(word in lowered for word in ("send", "approve", "deliver", "email"))
        for operation in operations.values():
            text = (operation.get("summary", "") + operation.get("operationId", "")).lower()
            assert "send" not in text and "approve" not in text


def test_application_code_has_no_email_transport() -> None:
    """Phase 4 must not be able to send email, even indirectly."""
    forbidden = {"smtplib", "email.mime", "email.message", "aiosmtplib", "sendgrid", "boto3"}
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(
                    name == bad or name.startswith(bad + ".") for bad in forbidden
                ), f"{path} imports {name}"
