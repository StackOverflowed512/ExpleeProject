"""Unit tests for discovery configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_search_defaults_to_tavily_without_a_key() -> None:
    settings = Settings(_env_file=None)
    assert settings.search_provider == "tavily"
    assert settings.tavily_api_key is None
    assert settings.search_timeout_seconds == 10.0
    assert settings.search_max_retries == 2


def test_tavily_is_allowed_without_a_key_at_startup() -> None:
    settings = Settings(_env_file=None, search_provider="tavily")
    assert settings.search_provider == "tavily"
    assert settings.tavily_api_key is None


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_tavily_key_is_normalized_to_none(blank: str) -> None:
    settings = Settings(
        _env_file=None, search_provider="tavily", tavily_api_key=blank
    )
    assert settings.tavily_api_key is None


def test_tavily_key_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")

    settings = Settings(_env_file=None, search_provider="tavily")

    assert settings.tavily_api_key is not None
    assert settings.tavily_api_key.get_secret_value() == "tvly-from-env"


def test_tavily_accepts_a_key_and_masks_it() -> None:
    settings = Settings(
        _env_file=None,
        search_provider="tavily",
        tavily_api_key="secret-value",
    )

    assert settings.search_provider == "tavily"
    assert settings.tavily_api_key is not None
    assert settings.tavily_api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(settings)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_provider="google")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search_timeout_seconds", 0),
        ("search_timeout_seconds", 61),
        ("search_max_retries", -1),
        ("search_max_retries", 6),
    ],
)
def test_provider_bounds_are_enforced(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
