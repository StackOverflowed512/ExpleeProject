"""Application configuration.

Settings are read from environment variables first, then from the project
root ``.env`` file, then from the defaults defined below. Environment
variable names are case-insensitive and mirror the field names
(``APP_ENV``, ``DATABASE_URL``, ``LOG_LEVEL``, ``SEARCH_PROVIDER``,
``TAVILY_API_KEY``, ``WEBSITE_FETCHER``, ``RESEARCH_*``, ``DRAFT_GENERATOR``,
``MISTRAL_*``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

AppEnvironment = Literal["development", "testing", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SearchProviderName = Literal["fake", "tavily"]
WebsiteFetcherName = Literal["fake", "httpx"]
DraftGeneratorName = Literal["fake", "mistral"]

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

    # --- Explorer website research --------------------------------------
    website_fetcher: WebsiteFetcherName = Field(
        default="httpx",
        description=(
            "Website fetcher used by research. 'httpx' fetches the stored "
            "candidate website directly (no search API, no credentials); "
            "'fake' performs no network access."
        ),
    )
    research_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        description="TCP/TLS connect timeout for one website request.",
    )
    research_read_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Read/write timeout between bytes of one website request.",
    )
    research_max_fetch_seconds: float = Field(
        default=20.0,
        gt=0,
        le=120,
        description="Wall-clock budget for one research fetch, redirects included.",
    )
    research_max_response_bytes: int = Field(
        default=2_000_000,
        ge=10_000,
        le=10_000_000,
        description="Maximum body bytes read from a page; larger pages are truncated.",
    )
    research_max_redirects: int = Field(
        default=5,
        ge=0,
        le=10,
        description="Maximum redirects followed; each target is revalidated.",
    )
    research_max_text_chars: int = Field(
        default=50_000,
        ge=1_000,
        le=200_000,
        description="Maximum characters of extracted text stored per page.",
    )
    research_freshness_hours: float = Field(
        default=24.0,
        ge=0,
        le=8760,
        description=(
            "Usable research younger than this is reused instead of fetching "
            "again, unless refresh is requested. 0 disables reuse."
        ),
    )

    # --- Outreach draft generation ----------------------------------------
    draft_generator: DraftGeneratorName = Field(
        default="mistral",
        description=(
            "Draft generator for outreach. 'mistral' calls the Mistral chat "
            "completions API and requires MISTRAL_API_KEY; 'fake' produces "
            "deterministic drafts without network access."
        ),
    )
    mistral_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Mistral API key. Optional at startup so the app and tests run "
            "without credentials; draft generation raises "
            "provider_not_configured when Mistral is selected without a key."
        ),
    )
    mistral_model: str = Field(
        default="mistral-small-latest",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Mistral model identifier used for draft generation.",
    )
    mistral_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        description="Timeout for a single Mistral HTTP request.",
    )
    mistral_max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Retries for transient Mistral failures (429, 5xx, timeouts).",
    )

    @model_validator(mode="after")
    def _check_research_timeouts(self) -> "Settings":
        if self.research_max_fetch_seconds < self.research_connect_timeout_seconds:
            raise ValueError(
                "RESEARCH_MAX_FETCH_SECONDS must be at least "
                "RESEARCH_CONNECT_TIMEOUT_SECONDS."
            )
        return self

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("tavily_api_key", "mistral_api_key", mode="before")
    @classmethod
    def _normalize_api_keys(cls, value: object) -> object:
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
