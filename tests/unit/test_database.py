"""Unit tests for :mod:`app.database`.

These tests build the engine and session factory but never open a database
connection.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.database import build_engine, build_session_factory, get_session
from app.config import Settings


class RecordingSession:
    """Minimal stand-in used to observe session lifecycle handling."""

    closed = False

    def close(self) -> None:
        self.closed = True


def _request_for(app: FastAPI) -> Request:
    scope = {
        "type": "http",
        "app": app,
        "headers": [],
        "query_string": b"",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "http_version": "1.1",
    }
    return Request(scope)


def test_build_engine_uses_the_configured_url(test_settings: Settings) -> None:
    engine = build_engine(test_settings)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "explee_test"
    finally:
        engine.dispose()


def test_sessions_are_bound_to_the_engine(test_settings: Settings) -> None:
    engine = build_engine(test_settings)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            assert session.bind is engine
            assert not session.in_transaction()
    finally:
        engine.dispose()


def test_session_factory_produces_independent_sessions(
    test_settings: Settings,
) -> None:
    engine = build_engine(test_settings)
    try:
        factory = build_session_factory(engine)
        first, second = factory(), factory()
        try:
            assert first is not second
        finally:
            first.close()
            second.close()
    finally:
        engine.dispose()


def test_get_session_closes_the_session(app: FastAPI) -> None:
    fake = RecordingSession()
    app.state.session_factory = lambda: fake

    dependency = get_session(_request_for(app))
    assert isinstance(dependency, Iterator)
    assert next(dependency) is fake
    assert fake.closed is False

    dependency.close()
    assert fake.closed is True


def test_get_session_closes_the_session_when_an_error_propagates(
    app: FastAPI,
) -> None:
    fake = RecordingSession()
    app.state.session_factory = lambda: fake

    dependency = get_session(_request_for(app))
    next(dependency)
    with pytest.raises(RuntimeError, match="handler failed"):
        dependency.throw(RuntimeError("handler failed"))
    assert fake.closed is True
