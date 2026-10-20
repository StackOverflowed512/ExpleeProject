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
from app.routers import discovery, health, research
from app.services.providers import (
    build_search_provider,
    build_website_fetcher,
)

logger = logging.getLogger(__name__)


def _build_lifespan(
    engine: Engine,
    *providers: object,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that logs startup/shutdown and releases resources."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s v%s", SERVICE_NAME, __version__)
        try:
            yield
        finally:
            for provider in providers:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
            engine.dispose()
            logger.info("Stopped %s", SERVICE_NAME)

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    search_provider: object | None = None,
    website_fetcher: object | None = None,
) -> FastAPI:
    """Create a configured FastAPI application.

    Passing explicit ``settings`` is how tests run without depending on the
    developer's ``.env`` file. ``search_provider`` and ``website_fetcher``
    let tests inject deterministic providers; by default the configured
    provider and bounded HTTP fetcher are built from settings. The engine is
    created eagerly but no database connection is opened until the first
    query.
    """
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    engine = build_engine(resolved_settings)
    session_factory = build_session_factory(engine)
    provider = (
        search_provider
        if search_provider is not None
        else build_search_provider(resolved_settings)
    )
    fetcher = (
        website_fetcher
        if website_fetcher is not None
        else build_website_fetcher(resolved_settings)
    )

    app = FastAPI(
        title="Explee API",
        summary="Personal sales intelligence and outreach platform.",
        version=__version__,
        lifespan=_build_lifespan(engine, provider, fetcher),
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.search_provider = provider
    app.state.website_fetcher = fetcher

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(research.router)

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
            "discovery": f"{base}/discovery/search",
        }

    return app


app = create_app()
