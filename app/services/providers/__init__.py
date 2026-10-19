"""Search provider package.

Discovery depends on :class:`app.services.providers.search.SearchProvider`;
concrete adapters are selected in :func:`build_search_provider`.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services.providers.brave import BraveSearchProvider
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.search import SearchProvider

__all__ = ["FakeSearchProvider", "SearchProvider", "build_search_provider"]

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
