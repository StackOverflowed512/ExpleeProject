"""FastAPI application factory and entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from app import SERVICE_NAME, __version__
from app.config import Settings, get_settings
from app.database import build_engine, build_session_factory
from app.errors import register_exception_handlers
from app.logging_config import configure_logging
from app.routers import (
    campaigns,
    discovery,
    drafts,
    health,
    prospects,
    qualification,
    research,
    ui,
)
from app.services.providers import (
    build_draft_generator,
    build_email_provider,
    build_search_provider,
    build_website_fetcher,
)
from app.web import csrf
from app.web.rendering import (
    STATIC_DIR,
    build_templates,
    render_error_page,
    security_headers,
)

logger = logging.getLogger(__name__)


def _build_lifespan(
    engine: Engine,
    *resources: object,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that logs startup/shutdown and releases resources.

    Each resource with a ``close()`` method (search provider, website
    fetcher, draft generator) is closed on shutdown, before the engine
    is disposed.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s v%s", SERVICE_NAME, __version__)
        try:
            yield
        finally:
            for resource in resources:
                close = getattr(resource, "close", None)
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
    draft_generator: object | None = None,
    email_provider: object | None = None,
) -> FastAPI:
    """Create a configured FastAPI application.

    Passing explicit ``settings`` is how tests run without depending on the
    developer's ``.env`` file. ``search_provider`` lets tests (and future
    callers) inject a provider; by default the provider selected by
    ``SEARCH_PROVIDER`` is built from settings. ``website_fetcher`` works the
    same way for research (``WEBSITE_FETCHER``), and ``draft_generator``
    for outreach drafts (``DRAFT_GENERATOR``), and ``email_provider`` for
    delivery (``EMAIL_PROVIDER``, default ``fake``). The engine is created eagerly
    but no database connection is opened until the first query.

    The server-rendered web interface is served under ``/ui`` with its assets
    under ``/static``; the JSON API routes are unchanged.
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
    generator = (
        draft_generator
        if draft_generator is not None
        else build_draft_generator(resolved_settings)
    )
    mailer = (
        email_provider
        if email_provider is not None
        else build_email_provider(resolved_settings)
    )

    app = FastAPI(
        title="Explee API",
        summary="Personal sales intelligence and outreach platform.",
        version=__version__,
        lifespan=_build_lifespan(engine, provider, fetcher, generator),
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.search_provider = provider
    app.state.website_fetcher = fetcher
    app.state.draft_generator = generator
    app.state.email_provider = mailer
    app.state.templates = build_templates()
    # Per-process CSRF signing secret: a restart invalidates open forms.
    app.state.csrf_secret = csrf.new_secret()
    app.state.html_error_renderer = render_error_page

    register_exception_handlers(app)
    app.middleware("http")(security_headers)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(research.router)
    app.include_router(qualification.router)
    app.include_router(prospects.router)
    app.include_router(campaigns.router)
    app.include_router(drafts.router)
    app.include_router(ui.router)

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
            "ui": f"{base}/ui/",
            "docs": f"{base}/docs",
            "health": f"{base}/health",
            "discovery": f"{base}/discovery/search",
        }

    return app


app = create_app()
