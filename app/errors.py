"""Consistent API error responses.

Every error response has the same envelope::

    {"error": {"code": "...", "message": "...", "details": null}}

Domain code raises :class:`AppError` (or a future subclass) with a stable
``code``; handlers registered by :func:`register_exception_handlers` turn it
into the envelope. Unhandled exceptions are logged and returned as a generic
500 without leaking internal details.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    logger.warning(
        "Application error on %s %s [%s]: %s",
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    return _error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    if isinstance(exc.detail, str):
        message, details = exc.detail, None
    else:
        message, details = "Request failed.", jsonable_encoder(exc.detail)
    return _error_response(
        exc.status_code,
        code,
        message,
        details,
        headers=exc.headers,
    )


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _error_response(
        422,
        "validation_error",
        "Request validation failed.",
        details=jsonable_encoder(exc.errors()),
    )


async def _handle_unexpected_error(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception(
        "Unhandled error on %s %s", request.method, request.url.path
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
