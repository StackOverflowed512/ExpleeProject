"""Template rendering, response security headers, and HTML error pages."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app import SERVICE_NAME, __version__
from app.web import csrf, presentation

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
UI_PREFIX = "/ui"

# Applied to every /ui page. No inline scripts or styles, no third-party
# origins, no framing, and forms may only post back to this server.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
_COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_duration(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "—"
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    return f"{milliseconds / 1000:.1f} s"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


def _asset_url(relative: str) -> str:
    """Static URL with a content hash, so browsers never use a stale copy."""
    try:
        digest = hashlib.sha256((STATIC_DIR / relative).read_bytes()).hexdigest()[:12]
    except OSError:
        return f"/static/{relative}"
    return f"/static/{relative}?v={digest}"


def build_templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = _format_datetime
    env.filters["duration"] = _format_duration
    env.filters["percent"] = _percent
    env.globals["status_view"] = presentation.status_view
    env.globals["failure_text"] = presentation.failure_text
    env.globals["service_name"] = SERVICE_NAME
    env.globals["version"] = __version__
    env.globals["asset_urls"] = {
        name: _asset_url(name) for name in ("css/app.css", "vendor/htmx.min.js")
    }
    return Jinja2Templates(env=env)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def wants_fragment(request: Request, target: str) -> bool:
    """True for an HTMX request that targets ``#target`` (not a boosted page)."""
    return (
        is_htmx(request)
        and request.headers.get("hx-target") == target
        and request.headers.get("hx-boosted") != "true"
    )


def _attention_count(request: Request) -> int | None:
    """Drafts whose delivery needs an operator, for the navigation badge."""
    from app.services.drafts import count_drafts_by_state

    try:
        with request.app.state.session_factory() as session:
            return count_drafts_by_state(session, ("delivery_uncertain", "sending"))
    except SQLAlchemyError:
        return None


def render(
    request: Request,
    template: str,
    context: Mapping[str, Any] | None = None,
    *,
    status_code: int = 200,
    nav: str = "",
) -> HTMLResponse:
    """Render ``template`` with the shared page context and CSRF cookie."""
    templates: Jinja2Templates = request.app.state.templates
    nonce = csrf.request_nonce(request)
    issued = nonce is None
    if nonce is None:
        nonce = csrf.new_nonce()
    page: dict[str, Any] = {
        "request": request,
        "csrf_token": csrf.token_for(request.app.state.csrf_secret, nonce),
        "csrf_field": csrf.FIELD_NAME,
        "csrf_header": csrf.HEADER_NAME,
        "nav": nav,
        "notice": presentation.notice_for(request.query_params.get("notice")),
        "attention_count": _attention_count(request),
        "delivery_mode": presentation.delivery_mode(
            request.app.state.settings, request.app.state.email_provider
        ),
    }
    page.update(context or {})
    response = templates.TemplateResponse(request, template, page, status_code=status_code)
    # Also set here: the 500 handler runs outside the header middleware.
    _apply_ui_headers(response)
    if issued:
        response.set_cookie(
            csrf.COOKIE_NAME,
            nonce,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path=csrf.COOKIE_PATH,
        )
    return response


def redirect(path: str, *, notice: str | None = None) -> RedirectResponse:
    """303 redirect to an internal UI path (never to a caller-supplied URL)."""
    if not is_safe_ui_path(path):
        raise ValueError(f"Refusing to redirect outside the UI: {path!r}")
    if notice is not None:
        if notice not in presentation.NOTICES:
            raise ValueError(f"Unknown notice code: {notice!r}")
        path = f"{path}?notice={notice}"
    return RedirectResponse(path, status_code=303)


def is_safe_ui_path(path: str) -> bool:
    """Only absolute paths inside /ui, with no scheme, host, or backslash tricks."""
    return (
        (path == UI_PREFIX or path.startswith(UI_PREFIX + "/"))
        and "//" not in path
        and "\\" not in path
        and "?" not in path
        and "#" not in path
        and all(32 < ord(char) < 127 for char in path)
    )


def _apply_ui_headers(response: Response) -> None:
    for name, value in _COMMON_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "HX-Request, HX-Target, HX-Boosted"


async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """HTTP middleware adding browser security headers."""
    response = await call_next(request)
    path = request.url.path
    if path == UI_PREFIX or path.startswith(UI_PREFIX + "/"):
        _apply_ui_headers(response)
    else:
        for name, value in _COMMON_HEADERS.items():
            response.headers.setdefault(name, value)
    return response


_ERROR_TITLES = {
    400: "The request could not be processed",
    403: "This action was refused",
    404: "Page not found",
    405: "This action is not available here",
    409: "This action conflicts with the current state",
    413: "The form is too large",
    415: "Unsupported form format",
    422: "Some values are not valid",
    500: "Something went wrong",
    503: "A service is unavailable",
}


def render_error_page(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    exc: BaseException | None = None,
) -> Response:
    """HTML error page for /ui requests. Never includes exception details."""
    if isinstance(exc, SQLAlchemyError):
        code = "database_unavailable"
        message = (
            "The database could not be reached or is not migrated. Check DATABASE_URL "
            "and run `alembic upgrade head`, then reload the page."
        )
        status_code = 503
    try:
        return render(
            request,
            "error.html",
            {
                "status_code": status_code,
                "title": _ERROR_TITLES.get(status_code, "Request failed"),
                "code": code,
                "message": message,
            },
            status_code=status_code,
        )
    except Exception:  # noqa: BLE001 - never fail while reporting a failure
        logger.exception("Could not render the error page")
        return HTMLResponse(
            "<!doctype html><title>Error</title><p>Something went wrong.</p>",
            status_code=status_code,
        )
