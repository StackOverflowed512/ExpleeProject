"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.config import Settings
from app.schemas.health import HealthResponse
from app.services import health as health_service

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    description=(
        "Reports that the application is running and returns its name, "
        "environment and version. Does not check the database."
    ),
    responses={
        500: {"description": "Unexpected server error (error envelope)."},
    },
)
def read_health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return health_service.get_health(settings)
