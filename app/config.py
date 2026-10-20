"""Application configuration.

Settings are read from environment variables first, then from the project
root ``.env`` file, then from the defaults defined below. Environment
variable names are case-insensitive and mirror the field names
(``APP_ENV``, ``DATABASE_URL``, ``LOG_LEVEL``, ``SEARCH_PROVIDER``,
``TAVILY_API_KEY``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

AppEnvironment = Literal["development", "testing", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SearchProviderName = Literal["fake", "tavily"]

DEFAULT_DATABASE_URL: Final = (
    "postgresql+psycopg://postgres:postgres@localhost:5432/explee"
)


class Settings(BaseSettings):
    """Runtime configuration for the application."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    app_env: AppEnvironment = Field(
        default="development",
        description="Deployment environment.",
    )
    database_url: PostgresDsn = Field(
        default=DEFAULT_DATABASE_URL,
        description="PostgreSQL connection URL using the psycopg 3 driver.",
    )
    log_level: LogLevel = Field(
        default="INFO",
        description="Root logging level.",
    )

    # --- Explorer search provider -------------------------------------
    search_provider: SearchProviderName = Field(
        default="tavily",
        description=(
            "Search provider used by Explorer discovery. 'tavily' calls the "
            "Tavily Search API and requires TAVILY_API_KEY; 'fake' returns no "
            "results and needs no credentials."
        ),
    )
    tavily_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Tavily Search API key. Optional at startup so the app and "
            "unrelated tests run without credentials; discovery raises a "
            "clear provider error when Tavily is selected without a key."
        ),
    )
    search_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Timeout for a single search provider HTTP request.",
    )
    search_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Retries for transient search provider failures.",
    )

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("tavily_api_key", mode="before")
    @classmethod
    def _normalize_tavily_api_key(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+psycopg":
            raise ValueError(
                "DATABASE_URL must use the 'postgresql+psycopg' driver (psycopg 3)."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
