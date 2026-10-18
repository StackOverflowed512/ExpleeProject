"""FastAPI application factory and entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import Engine

from app import SERVICE_NAME, __version__
from app.config import Settings, get_settings
from app.database import build_engine, build_session_factory
from app.errors import register_exception_handlers
from app.logging_config import configure_logging
from app.routers import health

logger = logging.getLogger(__name__)


def _build_lifespan(
    engine: Engine,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that logs startup/shutdown and disposes the engine."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s v%s", SERVICE_NAME, __version__)
        try:
            yield
        finally:
            engine.dispose()
            logger.info("Stopped %s", SERVICE_NAME)

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured FastAPI application.

    Passing explicit ``settings`` is how tests run without depending on the
    developer's ``.env`` file. The engine is created eagerly but no database
    connection is opened until the first query.
    """
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    engine = build_engine(resolved_settings)
    session_factory = build_session_factory(engine)

    app = FastAPI(
        title="Explee API",
        summary="Personal sales intelligence and outreach platform.",
        version=__version__,
        lifespan=_build_lifespan(engine),
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    register_exception_handlers(app)
    app.include_router(health.router)

    @app.get(
        "/",
        include_in_schema=False,
        response_model=dict[str, str],
        tags=["meta"],
    )
    def read_root(request: Request) -> dict[str, str]:
        base = str(request.base_url).rstrip("/")
        return {
            "service": SERVICE_NAME,
            "docs": f"{base}/docs",
            "health": f"{base}/health",
        }

    return app


app = create_app()
