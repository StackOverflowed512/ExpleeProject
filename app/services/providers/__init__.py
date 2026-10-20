"""Search provider package.

Discovery depends on :class:`app.services.providers.search.SearchProvider`;
concrete adapters are selected in :func:`build_search_provider`.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.search import SearchProvider
from app.services.providers.tavily import TavilySearchProvider

__all__ = [
    "FakeSearchProvider",
    "SearchProvider",
    "TavilySearchProvider",
    "build_search_provider",
]

logger = logging.getLogger(__name__)


def build_search_provider(settings: Settings) -> SearchProvider:
    """Build the provider selected by ``SEARCH_PROVIDER``.

    A missing Tavily key does not fail construction: the app can start and
    serve unrelated routes, and the provider raises a clear configuration
    error when discovery actually requests a search. Selection never falls
    back to another provider silently.
    """
    if settings.search_provider == "fake":
        return FakeSearchProvider()

    if settings.search_provider == "tavily":
        api_key = (
            settings.tavily_api_key.get_secret_value()
            if settings.tavily_api_key is not None
            else None
        )
        return TavilySearchProvider(
            api_key,
            timeout_seconds=settings.search_timeout_seconds,
            max_retries=settings.search_max_retries,
        )

    raise ValueError(f"Unsupported search provider: {settings.search_provider!r}")
