"""Consistent API error responses.

Every error response has the same envelope::

    {"error": {"code": "...", "message": "...", "details": null}}

Domain code raises :class:`AppError` (or a future subclass) with a stable
``code``; handlers registered by :func:`register_exception_handlers` turn it
into the envelope. Unhandled exceptions are logged and returned as a generic
500 without leaking internal details.

Requests under ``/ui`` (the server-rendered web interface) get an HTML error
page instead, through the renderer the application stores on
``app.state.html_error_renderer``. API responses are unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.errors import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
}


HtmlErrorRenderer = Callable[[Request, int, str, str, BaseException | None], Response]
_HTML_PREFIX = "/ui"


def _html_renderer(request: Request) -> HtmlErrorRenderer | None:
    path = request.url.path
    if path != _HTML_PREFIX and not path.startswith(_HTML_PREFIX + "/"):
        return None
    return getattr(request.app.state, "html_error_renderer", None)


class AppError(Exception):
    """Base class for domain errors that map onto HTTP responses."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "application_error",
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details)
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers=headers,
    )


async def _handle_app_error(request: Request, exc: AppError) -> Response:
    logger.warning(
        "Application error on %s %s [%s]: %s",
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    renderer = _html_renderer(request)
    if renderer is not None:
        return renderer(request, exc.status_code, exc.code, exc.message, None)
    return _error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> Response:
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    if isinstance(exc.detail, str):
        message, details = exc.detail, None
    else:
        message, details = "Request failed.", jsonable_encoder(exc.detail)
    renderer = _html_renderer(request)
    if renderer is not None:
        return renderer(request, exc.status_code, code, message, None)
    return _error_response(
        exc.status_code,
        code,
        message,
        details,
        headers=exc.headers,
    )


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    renderer = _html_renderer(request)
    if renderer is not None:
        return renderer(
            request,
            422,
            "validation_error",
            "The address contains a value that is not valid.",
            None,
        )
    return _error_response(
        422,
        "validation_error",
        "Request validation failed.",
        details=jsonable_encoder(exc.errors()),
    )


async def _handle_unexpected_error(
    request: Request, exc: Exception
) -> Response:
    logger.exception(
        "Unhandled error on %s %s", request.method, request.url.path
    )
    renderer = _html_renderer(request)
    if renderer is not None:
        return renderer(
            request, 500, "internal_error", "An unexpected error occurred.", exc
        )
    return _error_response(
        500,
        "internal_error",
        "An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the application's exception handlers to ``app``."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
