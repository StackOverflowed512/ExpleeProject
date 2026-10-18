"""Unit tests for the health service."""

from __future__ import annotations

from app import SERVICE_NAME, __version__
from app.config import Settings
from app.services.health import get_health


def test_get_health_reports_process_details(test_settings: Settings) -> None:
    result = get_health(test_settings)

    assert result.status == "ok"
    assert result.service == SERVICE_NAME
    assert result.environment == "testing"
    assert result.version == __version__


def test_get_health_does_not_require_a_database(test_settings: Settings) -> None:
    # No engine, no connection: the service only reads settings.
    unreachable = test_settings.model_copy(
        update={
            "database_url": "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"
        }
    )

    assert get_health(unreachable).status == "ok"
