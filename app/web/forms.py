"""Bounded, allow-listed parsing of browser form posts.

Uses only the standard library (``application/x-www-form-urlencoded``), so no
multipart dependency is needed. Every UI route declares the exact field names
it accepts: any other field is refused, so a crafted post cannot smuggle a
recipient, subject, or body into an approval or send request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from pydantic import ValidationError
from starlette.requests import Request

from app.web import csrf

MAX_FORM_BYTES = 64 * 1024
MAX_FORM_FIELDS = 200
_FORM_TYPE = "application/x-www-form-urlencoded"


class FormError(Exception):
    """The form could not be accepted; ``message`` is safe to display."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class FormData:
    """Parsed form fields; every value keeps its submission order."""

    fields: dict[str, list[str]] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        values = self.fields.get(name)
        return values[0] if values else None

    def getlist(self, name: str) -> list[str]:
        return list(self.fields.get(name, []))

    def has(self, name: str) -> bool:
        return name in self.fields


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_FORM_BYTES:
        raise FormError("The form is too large.", status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise FormError("The form is too large.", status_code=413)
    return bytes(body)


async def parse_form(request: Request) -> FormData:
    """Parse a bounded URL-encoded body (an empty body is an empty form)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    raw = await _read_body(request)
    if not raw and not content_type:
        return FormData()
    if content_type != _FORM_TYPE:
        raise FormError("Forms must be submitted as URL-encoded data.", status_code=415)
    try:
        text = raw.decode("utf-8")
        pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    except (UnicodeDecodeError, ValueError) as exc:
        raise FormError("The form could not be read.") from exc
    fields: dict[str, list[str]] = {}
    for name, value in pairs:
        fields.setdefault(name, []).append(value)
    return FormData(fields)


def check_allowed(form: FormData, allowed: Iterable[str]) -> None:
    """Refuse any field the route does not declare."""
    permitted = set(allowed) | {csrf.FIELD_NAME}
    unexpected = sorted(set(form.fields) - permitted)
    if unexpected:
        shown = ", ".join(name[:40] for name in unexpected[:5])
        raise FormError(f"The form contained unexpected fields: {shown}.", status_code=422)


async def read_form(request: Request, *, allowed: Iterable[str]) -> FormData:
    """Parse a URL-encoded form, refusing unknown fields (no CSRF check)."""
    form = await parse_form(request)
    check_allowed(form, allowed)
    return form


async def read_protected_form(request: Request, *, allowed: Iterable[str]) -> FormData:
    """Check the origin, parse the form, verify its CSRF token, then its fields.

    The token is checked before anything else about the form is reported.
    Raises :class:`csrf.CsrfError` or :class:`FormError`.
    """
    csrf.check_origin(request)
    form = await parse_form(request)
    submitted = form.get(csrf.FIELD_NAME) or request.headers.get(csrf.HEADER_NAME)
    csrf.verify(request, request.app.state.csrf_secret, submitted)
    check_allowed(form, allowed)
    return form


def field_errors(exc: ValidationError) -> dict[str, str]:
    """Map Pydantic errors to ``{field: message}`` (``"__all__"`` for the form)."""
    errors: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        key = str(location[0]) if location else "__all__"
        message = str(error.get("msg", "Invalid value."))
        message = message.removeprefix("Value error, ").removeprefix("Assertion failed, ")
        errors.setdefault(key, message[:300])
    return errors


def split_terms(value: str | None) -> list[str]:
    """Split a comma- or newline-separated text field into trimmed entries."""
    if not value:
        return []
    parts = value.replace("\r", "\n").replace("\n", ",").split(",")
    return [part.strip() for part in parts if part.strip()]
