"""Helpers shared by the web interface routes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import Query
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.web.csrf import CsrfError
from app.web.forms import FormData, FormError, read_protected_form
from app.web.rendering import render_error_page

PAGE_SIZE = 20
Offset = Annotated[int, Query(ge=0, le=10_000)]


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def page_links(total: int, offset: int, limit: int = PAGE_SIZE) -> dict[str, Any]:
    """Previous/next offsets for simple pagination."""
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "first": offset + 1 if total else 0,
        "last": min(offset + limit, total),
        "prev": max(offset - limit, 0) if offset > 0 else None,
        "next": offset + limit if offset + limit < total else None,
    }


async def handle_post(
    request: Request,
    allowed: Iterable[str],
    handler: Callable[[FormData], Response],
) -> Response:
    """Verify CSRF and parse the form, then run the blocking handler in a thread.

    Services are synchronous (database and provider I/O), so they never run
    on the event loop.
    """
    try:
        form = await read_protected_form(request, allowed=allowed)
    except CsrfError as exc:
        return await run_in_threadpool(
            render_error_page, request, 403, "csrf_failed", str(exc), None
        )
    except FormError as exc:
        return await run_in_threadpool(
            render_error_page, request, exc.status_code, "invalid_form", exc.message, None
        )
    return await run_in_threadpool(handler, form)


def optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
