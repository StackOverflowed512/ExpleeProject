"""Unit tests for website research settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_research_defaults_are_safe_and_need_no_credentials() -> None:
    settings = _settings()
    assert settings.website_fetcher == "httpx"
    assert settings.research_connect_timeout_seconds == 5.0
    assert settings.research_read_timeout_seconds == 10.0
    assert settings.research_max_fetch_seconds == 20.0
    assert settings.research_max_response_bytes == 2_000_000
    assert settings.research_max_redirects == 5
    assert settings.research_max_text_chars == 50_000
    assert settings.research_freshness_hours == 24.0


def test_research_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_FETCHER", "fake")
    monkeypatch.setenv("RESEARCH_MAX_REDIRECTS", "2")
    monkeypatch.setenv("RESEARCH_FRESHNESS_HOURS", "0")
    settings = _settings()
    assert settings.website_fetcher == "fake"
    assert settings.research_max_redirects == 2
    assert settings.research_freshness_hours == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("website_fetcher", "tavily"),
        ("research_connect_timeout_seconds", 0),
        ("research_connect_timeout_seconds", 31),
        ("research_read_timeout_seconds", -1),
        ("research_max_fetch_seconds", 121),
        ("research_max_response_bytes", 100),
        ("research_max_response_bytes", 50_000_000),
        ("research_max_redirects", -1),
        ("research_max_redirects", 11),
        ("research_max_text_chars", 10),
        ("research_freshness_hours", -1),
    ],
)
def test_invalid_research_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_total_budget_must_cover_connect_timeout() -> None:
    with pytest.raises(ValidationError):
        _settings(research_connect_timeout_seconds=10, research_max_fetch_seconds=5)
