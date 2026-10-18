"""Unit tests for :mod:`app.config`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_DATABASE_URL, Settings, get_settings
from tests.conftest import TEST_DATABASE_URL


def test_defaults_are_usable_without_a_dotenv_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert str(settings.database_url) == DEFAULT_DATABASE_URL
    assert str(settings.database_url) == DEFAULT_DATABASE_URL
    assert settings.log_level == "INFO"


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"
    assert str(settings.database_url) == TEST_DATABASE_URL
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TESTING", "testing"), ("Production", "production"), (" development ", "development")],
)
def test_app_env_is_case_insensitive(raw: str, expected: str) -> None:
    settings = Settings(_env_file=None, app_env=raw)
    assert settings.app_env == expected


@pytest.mark.parametrize("raw", ["qa", "dev", "", "prod"])
def test_invalid_app_env_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, app_env=raw)

    assert "app_env" in str(excinfo.value)


@pytest.mark.parametrize("raw", ["TRACE", "verbose", ""])
def test_invalid_log_level_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, log_level=raw)

    assert "log_level" in str(excinfo.value)


def test_non_postgres_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, database_url="sqlite:///./explee.db")

    assert "database_url" in str(excinfo.value)


def test_postgresql_without_explicit_driver_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://user:pass@localhost/db")


def test_malformed_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="not-a-url")


def test_get_settings_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = get_settings()
    assert get_settings() is first

    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert get_settings() is first  # cached until cleared

    get_settings.cache_clear()
    assert get_settings() is not first
    assert get_settings().log_level == "ERROR"
