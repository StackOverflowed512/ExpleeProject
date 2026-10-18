"""Unit tests for the shared error envelope."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import AppError, register_exception_handlers


class Payload(BaseModel):
    name: str
    score: int


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/missing")
    def missing() -> None:
        raise AppError("Nothing here.", code="not_found", status_code=404)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    @app.post("/validate")
    def validate(payload: Payload) -> Payload:
        return payload

    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_known_route_returns_json() -> None:
    app = _build_app()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "yes"}

    response = _client(app).get("/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": "yes"}


def test_http_404_uses_error_envelope() -> None:
    response = _client(_build_app()).get("/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "Not Found"
    assert body["error"]["details"] is None


def test_http_405_uses_error_envelope() -> None:
    response = _client(_build_app()).delete("/missing")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_validation_error_returns_field_details() -> None:
    response = _client(_build_app()).post("/validate", json={"name": "Ada", "score": "x"})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"]
    assert body["details"][0]["loc"] == ["body", "score"]


def test_app_error_uses_its_own_code_and_status() -> None:
    response = _client(_build_app()).get("/missing")

    assert response.status_code == 404
    body = response.json()["error"]
    assert body == {"code": "not_found", "message": "Nothing here.", "details": None}


def test_unhandled_exception_is_not_leaked() -> None:
    response = _client(_build_app()).get("/boom")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert "secret internals" not in body["message"]
    assert body["details"] is None
