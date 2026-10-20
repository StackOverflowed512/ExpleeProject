"""Tavily Search API adapter.

Contract (Tavily Search API, ``POST /search``):

* Endpoint: ``https://api.tavily.com/search``.
* Authentication: ``Authorization: Bearer <api key>``.
* Body: ``query``, ``max_results``, ``search_depth``, and the
  ``include_*`` flags used to keep responses small.
* Response: ``{"results": [{"title", "url", "content", "score", "id"}]}``.

Verify the current contract at
https://docs.tavily.com/documentation/api-reference/endpoint/search
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
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
PROVIDER_NAME = "tavily"
MAX_RESULTS = 20
MAX_PROVIDER_RESULT_ID_LENGTH = 255
MAX_RETRY_AFTER_SECONDS = 5.0


class _TavilyResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    url: PydanticHttpUrl | None = None
    content: str | None = None
    # Tavily documents an optional per-result identifier; keep it when it is
    # present so discovery can deduplicate on provider identity as well.
    id: str | None = None


class _TavilyEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Entries are validated one at a time so a single malformed result is
    # skipped instead of failing the whole search. ``results`` is required by
    # the documented response contract.
    results: list[object]


class TavilySearchProvider:
    """Search adapter backed by the Tavily Search API.

    A missing key is accepted at construction time so the application can
    start without search credentials. ``search`` raises
    :class:`ProviderConfigurationError` instead of contacting the provider,
    which keeps health checks and unrelated tests working without a key.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        key = api_key.strip() if isinstance(api_key, str) else ""
        self._api_key = key or None
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TavilySearchProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search(self, query: str, limit: int) -> list[SearchResultItem]:
        if not query.strip():
            raise ValueError("Search query must not be empty.")
        if self._api_key is None:
            raise ProviderConfigurationError(
                "Tavily search is selected but TAVILY_API_KEY is not configured."
            )
        count = max(1, min(limit, MAX_RESULTS))

        payload = self._request_payload(query.strip(), count)
        return self._normalize(payload)

    # -- HTTP ---------------------------------------------------------
    def _request_payload(self, query: str, count: int) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body = {
            "query": query,
            "max_results": count,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    TAVILY_SEARCH_URL,
                    headers=headers,
                    json=body,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                last_error = ProviderTimeoutError(
                    "The search provider timed out."
                )
                self._wait(attempt)
                continue
            except httpx.TransportError:
                last_error = ProviderUnavailableError(
                    "The search provider could not be reached."
                )
                self._wait(attempt)
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
                self._wait(attempt, response)
                continue
            if status in (432, 433):
                # Documented Tavily plan/quota limits; retrying will not help.
                raise ProviderUnavailableError(
                    "The search provider quota or plan limit was reached."
                )
            if status >= 500:
                last_error = ProviderUnavailableError(
                    "The search provider is temporarily unavailable."
                )
                self._wait(attempt, response)
                continue

            # Other 4xx: request-level rejection; retrying will not help.
            raise ProviderUnavailableError(
                "The search provider rejected the request."
            )

        assert last_error is not None
        raise last_error

    def _wait(
        self,
        attempt: int,
        response: httpx.Response | None = None,
    ) -> None:
        """Sleep before a retry, honoring a bounded ``Retry-After`` value."""
        if attempt >= self._max_retries:
            return

        delay = min(0.25 * (2**attempt), 2.0)
        retry_after = self._retry_after_seconds(response)
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        time.sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            # Tavily documents an integer number of seconds; ignore other
            # formats and fall back to the bounded exponential delay.
            return None
        return max(0.0, seconds)

    # -- Parsing ------------------------------------------------------
    def _normalize(self, payload: dict[str, object]) -> list[SearchResultItem]:
        try:
            envelope = _TavilyEnvelope.model_validate(payload)
        except ValidationError:
            raise ProviderUnavailableError(
                "The search provider returned an unexpected payload."
            ) from None

        items: list[SearchResultItem] = []
        for raw in envelope.results:
            if not isinstance(raw, dict):
                logger.debug("Skipping malformed %s result", PROVIDER_NAME)
                continue
            try:
                result = _TavilyResult.model_validate(raw)
            except ValidationError:
                logger.debug("Skipping malformed %s result", PROVIDER_NAME)
                continue

            title = result.title.strip() if result.title else ""
            if not title or result.url is None:
                logger.debug("Skipping malformed %s result", PROVIDER_NAME)
                continue

            snippet = result.content.strip() if result.content else None
            provider_result_id = result.id.strip() if result.id else None
            if (
                provider_result_id is not None
                and len(provider_result_id) > MAX_PROVIDER_RESULT_ID_LENGTH
            ):
                # The normalized schema bounds identifiers; never truncate an
                # opaque identifier because that could create false matches.
                provider_result_id = None

            items.append(
                SearchResultItem(
                    title=title[:300],
                    url=result.url,
                    snippet=snippet[:2000] if snippet else None,
                    provider=PROVIDER_NAME,
                    provider_result_id=provider_result_id,
                )
            )
        return items
