"""Company discovery endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import AppError
from app.schemas.discovery import DiscoveryRequest, DiscoveryResponse
from app.services.discovery import DiscoveryPersistenceError, run_discovery
from app.services.providers.errors import SearchProviderError
from app.services.providers.search import SearchProvider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discovery"])


@router.post(
    "/discovery/search",
    response_model=DiscoveryResponse,
    summary="Discover company candidates",
    description=(
        "Runs the configured search provider for a natural-language query, "
        "normalizes and deduplicates the results, persists new company "
        "candidates, and returns them with discovery counts."
    ),
    responses={
        422: {"description": "Request validation failed (error envelope)."},
        502: {
            "description": (
                "Search provider authentication failure or unavailable provider "
                "(error envelope)."
            )
        },
        503: {"description": "Search provider rate limited (error envelope)."},
        504: {"description": "Search provider timed out (error envelope)."},
        500: {"description": "Persistence failure (error envelope)."},
    },
)
def discover(
    payload: DiscoveryRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> DiscoveryResponse:
    provider: SearchProvider = request.app.state.search_provider
    try:
        return run_discovery(session, provider, payload.query, payload.limit)
    except ValueError as exc:
        # Defense in depth: schemas normally reject blank queries.
        raise AppError(
            str(exc), code="validation_error", status_code=422
        ) from exc
    except SearchProviderError as exc:
        logger.warning(
            "Search provider failure during discovery: %s", exc.code
        )
        raise AppError(
            str(exc), code=exc.code, status_code=exc.http_status
        ) from exc
    except DiscoveryPersistenceError as exc:
        raise AppError(
            str(exc),
            code="discovery_persistence_failed",
            status_code=500,
        ) from exc
