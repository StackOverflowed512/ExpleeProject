"""Shared test fixtures.

Tests never read the developer's ``.env`` file: settings are built explicitly
so results are identical on every machine. No test talks to a live database
or an external API.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.main import create_app

TEST_DATABASE_URL = "postgresql+psycopg://explee:explee@localhost:5432/explee_test"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent ``.env`` and process settings from leaking into unit tests."""
    for name in (
        "APP_ENV",
        "DATABASE_URL",
        "LOG_LEVEL",
        "SEARCH_PROVIDER",
        "TAVILY_API_KEY",
        "SEARCH_TIMEOUT_SECONDS",
        "SEARCH_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def test_settings() -> Settings:
    """Deterministic settings that never read the developer's ``.env``."""
    return Settings(
        _env_file=None,
        app_env="testing",
        database_url=TEST_DATABASE_URL,
        log_level="WARNING",
        search_provider="fake",
    )


@pytest.fixture
def build_app(test_settings: Settings) -> Callable[..., FastAPI]:
    """Factory for a fresh application instance per test."""

    def _build(**overrides: object) -> FastAPI:
        settings = (
            test_settings.model_copy(update=overrides) if overrides else test_settings
        )
        return create_app(settings)

    return _build


@pytest.fixture
def app(build_app: Callable[..., FastAPI]) -> FastAPI:
    return build_app()
