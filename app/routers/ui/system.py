"""System status: configuration summary without secrets, and database state."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.database import get_session
from app.routers.ui.common import settings_of
from app.services.health import get_health
from app.web import presentation
from app.web.rendering import render

router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def expected_migration_head() -> str | None:
    """The newest Alembic revision shipped with the code, if discoverable."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # noqa: BLE001 - informational only
        return None


def _database_revision(session: Session) -> tuple[bool, str | None]:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        session.rollback()
        return False, None
    try:
        return True, session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        session.rollback()
        return True, None


@router.get("/system")
def system_page(request: Request, session: Session = Depends(get_session)) -> Response:
    settings = settings_of(request)
    reachable, revision = _database_revision(session)
    head = expected_migration_head()
    return render(
        request,
        "system/index.html",
        {
            "health": get_health(settings),
            # A curated view: never the database URL, keys, or passwords.
            "config": {
                "search_provider": settings.search_provider,
                "website_fetcher": settings.website_fetcher,
                "research_freshness_hours": settings.research_freshness_hours,
                "draft_generator": settings.draft_generator,
                "mistral_model": settings.mistral_model,
                "email_provider": settings.email_provider,
                "email_dry_run": settings.email_dry_run,
                "email_sender": settings.email_sender,
                "smtp_host": settings.smtp_host,
                "smtp_port": settings.smtp_port,
                "smtp_security": settings.smtp_security,
                "approval_actor": settings.approval_actor,
            },
            # Only whether a secret is set, never its value.
            "tavily_key_set": settings.tavily_api_key is not None,
            "mistral_key_set": settings.mistral_api_key is not None,
            "smtp_login_set": settings.smtp_username is not None,
            "mode": presentation.delivery_mode(settings, request.app.state.email_provider),
            "db_reachable": reachable,
            "db_revision": revision,
            "expected_head": head,
            "migrations_current": revision is not None and revision == head,
        },
        nav="system",
    )
