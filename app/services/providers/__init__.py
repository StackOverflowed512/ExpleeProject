"""External provider package.

Discovery depends on :class:`app.services.providers.search.SearchProvider`;
concrete adapters are selected in :func:`build_search_provider`. Research
depends on :class:`app.services.providers.website.WebsiteFetcher`, selected
in :func:`build_website_fetcher`. Outreach depends on
:class:`app.services.providers.drafts.DraftGenerator`, selected in
:func:`build_draft_generator`.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services.providers.drafts import DraftGenerator
from app.services.providers.drafts_fake import FakeDraftGenerator
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.mistral import MistralDraftGenerator
from app.services.providers.search import SearchProvider
from app.services.providers.tavily import TavilySearchProvider
from app.services.providers.website import WebsiteFetcher
from app.services.providers.website_fake import FakeWebsiteFetcher
from app.services.providers.website_httpx import HttpxWebsiteFetcher

__all__ = [
    "DraftGenerator",
    "FakeDraftGenerator",
    "FakeSearchProvider",
    "FakeWebsiteFetcher",
    "HttpxWebsiteFetcher",
    "MistralDraftGenerator",
    "SearchProvider",
    "TavilySearchProvider",
    "WebsiteFetcher",
    "build_draft_generator",
    "build_search_provider",
    "build_website_fetcher",
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


def build_website_fetcher(settings: Settings) -> WebsiteFetcher:
    """Build the fetcher selected by ``WEBSITE_FETCHER``.

    Neither adapter needs credentials. Research never uses a search provider
    to fetch pages, so it consumes no search API quota.
    """
    if settings.website_fetcher == "fake":
        return FakeWebsiteFetcher()

    if settings.website_fetcher == "httpx":
        return HttpxWebsiteFetcher(
            connect_timeout_seconds=settings.research_connect_timeout_seconds,
            read_timeout_seconds=settings.research_read_timeout_seconds,
            max_fetch_seconds=settings.research_max_fetch_seconds,
            max_response_bytes=settings.research_max_response_bytes,
            max_redirects=settings.research_max_redirects,
            max_text_chars=settings.research_max_text_chars,
        )

    raise ValueError(f"Unsupported website fetcher: {settings.website_fetcher!r}")


def build_draft_generator(settings: Settings) -> DraftGenerator:
    """Build the generator selected by ``DRAFT_GENERATOR``.

    A missing Mistral key does not fail construction: the app starts and
    serves unrelated routes, and generation raises a configuration error.
    Selection never falls back to the fake generator silently.
    """
    if settings.draft_generator == "fake":
        return FakeDraftGenerator()

    if settings.draft_generator == "mistral":
        api_key = (
            settings.mistral_api_key.get_secret_value()
            if settings.mistral_api_key is not None
            else None
        )
        return MistralDraftGenerator(
            api_key,
            model=settings.mistral_model,
            timeout_seconds=settings.mistral_timeout_seconds,
            max_retries=settings.mistral_max_retries,
        )

    raise ValueError(f"Unsupported draft generator: {settings.draft_generator!r}")
