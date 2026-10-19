"""Unit tests for the search provider abstraction and Brave adapter.

All HTTP is served by :class:`httpx.MockTransport`; no real API is called.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.config import Settings
from app.schemas.search import SearchResultItem
from app.services.providers import FakeSearchProvider, build_search_provider
from app.services.providers.brave import BRAVE_SEARCH_URL, BraveSearchProvider
from app.services.providers.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(
    handler: Handler, *, max_retries: int = 0
) -> tuple[BraveSearchProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        BraveSearchProvider(
            "test-key",
            timeout_seconds=1.0,
            max_retries=max_retries,
            client=client,
        ),
        client,
    )


def _ok_payload() -> dict[str, object]:
    return {
        "web": {
            "results": [
                {
                    "title": "Acme Corp",
                    "url": "https://www.acme.com/",
                    "description": "Acme builds widgets.",
                    "profile": {"name": "Acme"},
                }
            ]
        }
    }


def test_request_is_constructed_from_documented_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok_payload())

    provider, _ = _provider(handler)
    provider.search("acme widgets", 7)

    request = captured[0]
    assert request.method == "GET"
    assert str(request.url).startswith(BRAVE_SEARCH_URL)
    assert request.headers["X-Subscription-Token"] == "test-key"
    assert request.headers["Accept"] == "application/json"
    assert request.url.params["q"] == "acme widgets"
    assert request.url.params["count"] == "7"


def test_successful_response_is_normalized() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, json=_ok_payload()))

    [item] = provider.search("acme", 5)

    assert isinstance(item, SearchResultItem)
    assert item.title == "Acme Corp"
    assert str(item.url) == "https://www.acme.com/"
    assert item.snippet == "Acme builds widgets."
    assert item.provider == "brave"
    assert item.provider_result_id is None


def test_empty_web_results_return_empty_list() -> None:
    provider, _ = _provider(
        lambda _: httpx.Response(200, json={"web": {"results": []}})
    )
    assert provider.search("nothing", 5) == []


def test_missing_web_block_treated_as_no_results() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, json={"query": {}}))
    assert provider.search("nothing", 5) == []


def test_malformed_results_are_skipped_individually() -> None:
    payload = {
        "web": {
            "results": [
                {"title": "No URL", "description": "x"},
                {"url": "https://missing-title.com"},
                {"title": "Good", "url": "https://good.com", "description": None},
            ]
        }
    }
    provider, _ = _provider(lambda _: httpx.Response(200, json=payload))

    [item] = provider.search("q", 5)
    assert item.title == "Good"


def test_non_json_body_is_provider_unavailable() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_wrong_payload_shape_is_provider_unavailable() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure(status: int) -> None:
    provider, _ = _provider(lambda _: httpx.Response(status, json={}))
    with pytest.raises(ProviderAuthenticationError):
        provider.search("q", 5)


def test_server_error_is_unavailable() -> None:
    provider, _ = _provider(lambda _: httpx.Response(500, json={}))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_client_error_is_unavailable() -> None:
    provider, _ = _provider(lambda _: httpx.Response(400, json={}))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_rate_limit_is_retried_then_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.providers.brave.time.sleep", lambda _: None)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={})

    provider, _ = _provider(handler, max_retries=2)
    with pytest.raises(ProviderRateLimitError):
        provider.search("q", 5)
    assert len(calls) == 3


def test_transient_server_error_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.providers.brave.time.sleep", lambda _: None)
    responses = [httpx.Response(503), httpx.Response(200, json=_ok_payload())]

    provider, _ = _provider(lambda _: responses.pop(0), max_retries=1)
    assert len(provider.search("q", 5)) == 1


def test_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.providers.brave.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider, _ = _provider(handler, max_retries=1)
    with pytest.raises(ProviderTimeoutError):
        provider.search("q", 5)


def test_transport_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.providers.brave.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(handler, max_retries=0)
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_errors_never_include_the_api_key() -> None:
    secret = "super-secret-key"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BraveSearchProvider(secret, client=client)
    with pytest.raises(ProviderAuthenticationError) as excinfo:
        provider.search("q", 5)
    assert secret not in str(excinfo.value)


def test_empty_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        BraveSearchProvider("   ")


def test_limit_is_bounded_and_empty_query_rejected() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"web": {"results": []}})

    provider, _ = _provider(handler)
    provider.search("q", 999)
    assert captured[0].url.params["count"] == "20"

    with pytest.raises(ValueError):
        provider.search("   ", 5)


# -- factory ---------------------------------------------------------------


def test_factory_returns_fake_by_default() -> None:
    settings = Settings(_env_file=None, search_provider="fake")
    assert isinstance(build_search_provider(settings), FakeSearchProvider)


def test_factory_rejects_brave_without_key() -> None:
    with pytest.raises(Exception):
        Settings(_env_file=None, search_provider="brave", search_api_key=None)


def test_factory_builds_brave_with_secret() -> None:
    settings = Settings(
        _env_file=None, search_provider="brave", search_api_key="k"
    )
    provider = build_search_provider(settings)
    assert isinstance(provider, BraveSearchProvider)


def test_fake_provider_respects_limit() -> None:
    results = [
        SearchResultItem(title=f"Co {i}", url=f"https://co{i}.com", provider="fake")
        for i in range(3)
    ]
    provider = FakeSearchProvider(results=results)
    assert len(provider.search("q", 2)) == 2
