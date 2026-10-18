"""Brave Search API adapter.

Contract (Brave Web Search API, ``GET /res/v1/web/search``):

* Authentication: ``X-Subscription-Token`` header carrying the API key.
* Parameters: ``q`` (query), ``count`` (results, 1-20), ``result_filter=web``.
* Response: ``{"web": {"results": [{"title", "url", "description",
  "profile": {"name"}}]}}``.

Verify the current contract at
https://api-dashboard.search.brave.com/app/documentation/web-search
before relying on it in production. This adapter is intentionally defensive:
malformed individual results are skipped, and only documented fields are
read. It never logs the API key or the raw provider response.
"""

from __future__ import annotations

import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic import HttpUrl as PydanticHttpUrl

from app.schemas.search import SearchResultItem
from app.services.providers.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
PROVIDER_NAME = "brave"
MAX_COUNT = 20


class _BraveProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None


class _BraveResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    url: PydanticHttpUrl | None = None
    description: str | None = None
    profile: _BraveProfile | None = None
    # Brave does not document a stable per-result id; if one starts appearing
    # it can be mapped here without touching discovery logic.


class _BraveWeb(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Individual entries are validated one at a time so a single malformed
    # result is skipped instead of failing the whole search.
    results: list[dict[str, object]] | None = None


class _BraveEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    web: _BraveWeb | None = None


class BraveSearchProvider:
    """Search adapter backed by the Brave Search API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Brave Search requires a non-empty API key.")
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BraveSearchProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search(self, query: str, limit: int) -> list[SearchResultItem]:
        if not query.strip():
            raise ValueError("Search query must not be empty.")
        count = max(1, min(limit, MAX_COUNT))

        payload = self._request_payload(query, count)
        return self._normalize(payload)

    # -- HTTP ---------------------------------------------------------
    def _request_payload(self, query: str, count: int) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "count": str(count),
            "result_filter": "web",
            "text_decorations": "false",
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(
                    BRAVE_SEARCH_URL,
                    headers=headers,
                    params=params,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                last_error = ProviderTimeoutError(
                    "The search provider timed out."
                )
                self._backoff(attempt)
                continue
            except httpx.TransportError:
                last_error = ProviderUnavailableError(
                    "The search provider could not be reached."
                )
                self._backoff(attempt)
                continue

            status = response.status_code
            if status == 200:
                try:
                    parsed = response.json()
                except ValueError:
                    raise ProviderUnavailableError(
                        "The search provider returned a non-JSON response."
                    ) from None
                if not isinstance(parsed, dict):
                    raise ProviderUnavailableError(
                        "The search provider returned an unexpected payload."
                    )
                return parsed

            if status in (401, 403):
                raise ProviderAuthenticationError(
                    "The search provider rejected the configured credentials."
                )
            if status == 429:
                last_error = ProviderRateLimitError(
                    "The search provider rate limit was exceeded."
                )
                self._backoff(attempt)
                continue
            if status >= 500:
                last_error = ProviderUnavailableError(
                    "The search provider is temporarily unavailable."
                )
                self._backoff(attempt)
                continue

            # Other 4xx: request-level rejection; retrying will not help.
            raise ProviderUnavailableError(
                "The search provider rejected the request."
            )

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            time.sleep(min(0.25 * (2**attempt), 2.0))

    # -- Parsing ------------------------------------------------------
    def _normalize(self, payload: dict[str, object]) -> list[SearchResultItem]:
        try:
            envelope = _BraveEnvelope.model_validate(payload)
        except ValidationError:
            raise ProviderUnavailableError(
                "The search provider returned an unexpected payload."
            ) from None

        if envelope.web is None or envelope.web.results is None:
            # A well-formed response with no web block means no results.
            return []

        items: list[SearchResultItem] = []
        for raw in envelope.web.results:
            try:
                result = _BraveResult.model_validate(raw)
            except ValidationError:
                logger.debug("Skipping malformed %s result", PROVIDER_NAME)
                continue
            if not result.title or not result.title.strip() or result.url is None:
                logger.debug("Skipping malformed %s result", PROVIDER_NAME)
                continue
            snippet = result.description.strip() if result.description else None
            items.append(
                SearchResultItem(
                    title=result.title.strip()[:300],
                    url=result.url,
                    snippet=snippet[:2000] if snippet else None,
                    provider=PROVIDER_NAME,
                    provider_result_id=None,
                )
            )
        return items
