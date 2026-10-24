"""Validation tests for qualification and prospect endpoints. No database needed.

Every request here is rejected during request validation, before any
service or database code runs.
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
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


@pytest.mark.parametrize("candidate_id", ["0", "-1", "abc", "2147483648"])
@pytest.mark.parametrize(
    ("method", "suffix"),
    [("POST", "qualify"), ("GET", "qualifications"), ("POST", "save")],
)
def test_invalid_candidate_ids(
    client: TestClient, candidate_id: str, method: str, suffix: str
) -> None:
    _assert_validation_error(client.request(method, f"/companies/{candidate_id}/{suffix}"))


@pytest.mark.parametrize(
    "criteria",
    [
        {"target_keywords": []},
        {"target_keywords": ["a"]},
        {"target_keywords": ["<b>"]},
        {"target_keywords": ["saas"], "threshold": 0},
        {"target_keywords": ["saas"], "threshold": 101},
        {"target_keywords": ["saas"], "min_evidence_coverage": 0},
        {"target_keywords": ["saas"], "target_tlds": ["co.uk"]},
        {"target_keywords": ["saas"], "weights": {"industry_fit": 1}},
    ],
)
def test_invalid_criteria_are_rejected(client: TestClient, criteria: dict[str, object]) -> None:
    _assert_validation_error(client.post("/companies/1/qualify", json={"criteria": criteria}))


@pytest.mark.parametrize(
    "body",
    [{"url": "https://acme.com"}, {"criteria": "saas"}, []],
)
def test_qualify_body_rejects_unknown_shapes(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/companies/1/qualify", json=body))


@pytest.mark.parametrize(
    "body",
    [{"qualification_id": 0}, {"qualification_id": "x"}, {"candidate_id": 2}],
)
def test_save_body_is_validated(client: TestClient, body: object) -> None:
    _assert_validation_error(client.post("/companies/1/save", json=body))


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 10_001},
        {"min_score": -1},
        {"max_score": 101},
        {"min_score": 80, "max_score": 20},
        {"sort": "random"},
        {"q": "a"},
        {"q": "x" * 101},
        {"saved_after": "not-a-date"},
        {"saved_after": "2026-10-24T10:00:00"},
        {"saved_after": "2026-10-25T00:00:00Z", "saved_before": "2026-10-24T00:00:00Z"},
        {"unknown": "1"},
    ],
)
def test_invalid_prospect_filters(client: TestClient, params: dict[str, object]) -> None:
    _assert_validation_error(client.get("/prospects", params=params))


@pytest.mark.parametrize("prospect_id", ["0", "abc", "2147483648"])
def test_invalid_prospect_id(client: TestClient, prospect_id: str) -> None:
    _assert_validation_error(client.get(f"/prospects/{prospect_id}"))


def test_openapi_documents_phase3_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "post" in paths["/companies/{candidate_id}/qualify"]
    assert "get" in paths["/companies/{candidate_id}/qualifications"]
    save = paths["/companies/{candidate_id}/save"]["post"]["responses"]
    assert {"200", "201", "404", "409", "422"} <= set(save)
    assert "get" in paths["/prospects"]
    assert "get" in paths["/prospects/{prospect_id}"]
    criteria = schema["components"]["schemas"]["QualificationCriteria"]
    assert criteria["additionalProperties"] is False
