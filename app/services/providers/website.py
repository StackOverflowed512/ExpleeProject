"""Website fetcher interface and normalized result contract.

Research business logic depends only on :class:`WebsiteFetcher`; concrete
adapters live in sibling modules. A fetch is expected to be bounded and
failure is a normal, typed outcome rather than an exception, so research can
store an auditable failure record without losing earlier successful data.

Raw HTTP details (headers, cookies, response bodies, exception text) are not
part of this contract. Only the values research needs are exposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class FetchOutcome(StrEnum):
    """Outcome of one website fetch attempt."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class FetchFailureCategory(StrEnum):
    """Stable, safe failure categories for auditing.

    Categories are deliberately coarse. They never contain exception text,
    response bodies, or resolved addresses.
    """

    INVALID_URL = "invalid_url"
    BLOCKED_DESTINATION = "blocked_destination"
    DNS_FAILURE = "dns_failure"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    REDIRECT_LOOP = "redirect_loop"
    HTTP_ERROR = "http_error"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"


@dataclass(frozen=True, slots=True)
class WebsiteFetchResult:
    """Normalized result of one bounded website fetch.

    ``final_url`` is the last URL reached through permitted redirects. It is
    ``None`` when the initial URL was invalid before any request was made.
    ``text`` is often empty on failure; ``failure_category`` then explains why.
    ``content_type`` is the response header, capped for storage, not a parsed
    value.
    """

    requested_url: str
    fetched_at: datetime
    outcome: FetchOutcome
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    page_title: str | None = None
    text: str = ""
    failure_category: FetchFailureCategory | None = None
    truncated: bool = False


@runtime_checkable
class WebsiteFetcher(Protocol):
    """Contract every website fetch adapter must satisfy."""

    name: str

    def fetch(self, url: str) -> WebsiteFetchResult:
        """Fetch ``url`` and return a normalized, bounded result.

        Implementations must validate the destination before requesting it,
        revalidate redirect destinations, and never invent page content.
        Expected failures are returned as ``outcome=FAILED`` results; only
        programming errors should raise.
        """
        ...
