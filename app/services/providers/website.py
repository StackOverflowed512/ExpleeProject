"""Website fetcher interface and normalized fetch result contract.

Research business logic depends only on :class:`WebsiteFetcher`; adapters
live in sibling modules (``website_httpx``, ``website_fake``) and are
selected in :func:`app.services.providers.build_website_fetcher`.

A fetch never raises for expected failures. It returns a
:class:`FetchResult` whose ``outcome`` and ``failure`` describe what
happened using stable, safe values; raw responses and exception messages
never leave the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class FetchOutcome(StrEnum):
    """What a fetch produced."""

    SUCCESS = "success"
    """An HTML page was fetched completely and contained readable text."""
    PARTIAL = "partial"
    """Readable text was extracted, but the body or text hit a size limit."""
    EMPTY = "empty"
    """An HTML page was fetched but contained no readable text."""
    FAILED = "failed"
    """No usable page was obtained; ``failure`` says why."""


USABLE_OUTCOMES = frozenset({FetchOutcome.SUCCESS, FetchOutcome.PARTIAL})


class FetchFailure(StrEnum):
    """Safe failure categories surfaced in research records and the API."""

    INVALID_URL = "invalid_url"
    DISALLOWED_SCHEME = "disallowed_scheme"
    CREDENTIALS_IN_URL = "credentials_in_url"
    DISALLOWED_PORT = "disallowed_port"
    BLOCKED_DESTINATION = "blocked_destination"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    INVALID_REDIRECT = "invalid_redirect"
    HTTP_ERROR = "http_error"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    MALFORMED_RESPONSE = "malformed_response"


@dataclass(frozen=True)
class FetchResult:
    """Normalized result of fetching one website URL.

    ``requested_url`` is the URL the caller asked for; ``final_url`` is the
    last URL actually requested after permitted redirects (``None`` when no
    request was made). Unknown values stay ``None``.
    """

    requested_url: str
    fetched_at: datetime
    outcome: FetchOutcome
    fetcher: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    page_title: str | None = None
    text: str | None = None
    content_truncated: bool = False
    failure: FetchFailure | None = None

    @property
    def usable(self) -> bool:
        return self.outcome in USABLE_OUTCOMES


@runtime_checkable
class WebsiteFetcher(Protocol):
    """Contract every website fetching adapter must satisfy."""

    name: str

    def fetch(self, url: str) -> FetchResult:
        """Fetch ``url`` and return a normalized result.

        Implementations must validate the destination (see
        :mod:`app.services.providers.url_safety`), must not raise for
        network, HTTP, or content failures, and must not fabricate content.
        """
        ...
