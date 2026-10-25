"""CSRF protection for browser form posts under ``/ui``.

A signed double-submit token:

* The browser holds a random nonce in the ``explee_csrf`` cookie
  (``HttpOnly``, ``SameSite=Strict``, path ``/ui``, kept for 180 days so
  tabs restored together reuse it rather than each issuing a new one).
* Every page embeds ``HMAC-SHA256(secret, nonce)`` in each form's hidden
  ``csrf_token`` field, and in an ``X-CSRF-Token`` header for HTMX requests.
* A state-changing request is accepted only when the submitted token matches
  the HMAC of the cookie's nonce. Another site can neither read the token
  nor, because of ``SameSite=Strict``, make the browser send the cookie.

The secret is random per application process (``create_app``), so a server
restart invalidates open forms; the user reloads the page. In addition, a
request whose ``Origin`` is not this server, or whose ``Sec-Fetch-Site`` says
it came from another site, is refused before the body is read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from starlette.requests import Request

COOKIE_NAME = "explee_csrf"
FIELD_NAME = "csrf_token"
HEADER_NAME = "X-CSRF-Token"
COOKIE_PATH = "/ui"
COOKIE_MAX_AGE = 180 * 24 * 3600  # persistent; see rendering.render
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOKEN_CONTEXT = b"explee-csrf-v1:"
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


class CsrfError(Exception):
    """The request failed CSRF validation; the message is safe to display."""


def new_secret() -> bytes:
    return secrets.token_bytes(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def valid_nonce(value: str | None) -> bool:
    return value is not None and bool(_NONCE_PATTERN.match(value))


def token_for(secret: bytes, nonce: str) -> str:
    digest = hmac.new(secret, _TOKEN_CONTEXT + nonce.encode("ascii"), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def request_nonce(request: Request) -> str | None:
    """The cookie nonce, if present and well formed."""
    value = request.cookies.get(COOKIE_NAME)
    return value if valid_nonce(value) else None


def check_origin(request: Request) -> None:
    """Refuse requests that a browser marks as coming from another site."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in _ALLOWED_FETCH_SITES:
        raise CsrfError("This request came from another site and was refused.")
    origin = request.headers.get("origin")
    if origin is None:
        return
    expected = f"{request.url.scheme}://{request.url.netloc}".lower()
    if origin.lower() != expected:
        raise CsrfError("This request came from another site and was refused.")


def verify(request: Request, secret: bytes, submitted: str | None) -> None:
    """Raise :class:`CsrfError` unless ``submitted`` matches the cookie nonce."""
    nonce = request_nonce(request)
    if nonce is None or not submitted:
        raise CsrfError(
            "This form has expired or is missing its security token. "
            "Reload the page and try again."
        )
    if not hmac.compare_digest(token_for(secret, nonce), submitted):
        raise CsrfError(
            "This form's security token is not valid. Reload the page and try again."
        )
