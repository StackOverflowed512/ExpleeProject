"""Provider package.

Discovery depends on :class:`app.services.providers.search.SearchProvider`;
research depends on
:class:`app.services.providers.website.WebsiteFetcher`. Concrete adapters are
selected by the small factories below.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services.providers.brave import BraveSearchProvider
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.fake_website import FakeWebsiteFetcher
from app.services.providers.http_website import HttpWebsiteFetcher
from app.services.providers.search import SearchProvider
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetcher,
    WebsiteFetchResult,
)

__all__ = [
    "FakeSearchProvider",
    "FakeWebsiteFetcher",
    "FetchFailureCategory",
    "FetchOutcome",
    "HttpWebsiteFetcher",
    "SearchProvider",
    "WebsiteFetchResult",
    "WebsiteFetcher",
    "build_search_provider",
    "build_website_fetcher",
]

logger = logging.getLogger(__name__)


def build_search_provider(settings: Settings) -> SearchProvider:
    """Build the provider selected by ``SEARCH_PROVIDER``.

    Raises ``ValueError`` when a real provider is selected without its
    credentials; settings validation normally catches this at startup.
    """
    if settings.search_provider == "fake":
        return FakeSearchProvider()

    if settings.search_provider == "brave":
        api_key = settings.search_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            raise ValueError(
                "SEARCH_API_KEY is required when SEARCH_PROVIDER=brave."
            )
        return BraveSearchProvider(
            api_key.get_secret_value(),
            timeout_seconds=settings.search_timeout_seconds,
            max_retries=settings.search_max_retries,
        )

    raise ValueError(f"Unsupported search provider: {settings.search_provider!r}")


def build_website_fetcher(settings: Settings) -> WebsiteFetcher:
    """Build the bounded HTTP website fetcher from application settings."""
    return HttpWebsiteFetcher(
        connect_timeout_seconds=settings.website_fetch_connect_timeout_seconds,
        read_timeout_seconds=settings.website_fetch_read_timeout_seconds,
        total_timeout_seconds=settings.website_fetch_total_timeout_seconds,
        max_bytes=settings.website_fetch_max_bytes,
        max_redirects=settings.website_fetch_max_redirects,
        max_text_chars=settings.website_fetch_max_text_chars,
    )
