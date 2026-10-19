"""Unit tests for discovery configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_search_defaults_to_fake_provider() -> None:
    settings = Settings(_env_file=None)
    assert settings.search_provider == "fake"
    assert settings.search_api_key is None
    assert settings.search_timeout_seconds == 10.0
    assert settings.search_max_retries == 2


def test_brave_requires_an_api_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, search_provider="brave")

    assert "SEARCH_API_KEY" in str(excinfo.value)


def test_brave_rejects_blank_api_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_provider="brave", search_api_key="   ")


def test_brave_accepts_a_key_and_masks_it() -> None:
    settings = Settings(
        _env_file=None, search_provider="brave", search_api_key="secret-value"
    )

    assert settings.search_provider == "brave"
    assert settings.search_api_key is not None
    assert settings.search_api_key.get_secret_value() == "secret-value"
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
