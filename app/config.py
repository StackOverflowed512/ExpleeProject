"""Application configuration.

Settings are read from environment variables first, then from the project
root ``.env`` file, then from the defaults defined below. Environment
variable names are case-insensitive and mirror the field names
(``APP_ENV``, ``DATABASE_URL``, ``LOG_LEVEL``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

AppEnvironment = Literal["development", "testing", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

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

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

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
