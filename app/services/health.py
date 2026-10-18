"""Health reporting."""

from __future__ import annotations

from app import SERVICE_NAME, __version__
from app.config import Settings
from app.schemas.health import HealthResponse


def get_health(settings: Settings) -> HealthResponse:
    """Report the application's liveness.

    Deliberately dependency-free: readiness of PostgreSQL and external
    providers will be introduced with the features that need them.
    """
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        environment=settings.app_env,
        version=__version__,
    )
