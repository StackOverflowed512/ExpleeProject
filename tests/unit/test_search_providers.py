"""Unit tests for the search provider abstraction and Tavily adapter.

All HTTP is served by :class:`httpx.MockTransport`; no real API is called.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.config import Settings
from app.schemas.search import SearchResultItem
from app.services.providers import (
    FakeSearchProvider,
    TavilySearchProvider,
    build_search_provider,
)
from app.services.providers.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.providers.tavily import TAVILY_SEARCH_URL

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(
    handler: Handler,
    *,
    max_retries: int = 0,
    api_key: str | None = "test-key",
) -> tuple[TavilySearchProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        TavilySearchProvider(
            api_key,
            timeout_seconds=1.0,
            max_retries=max_retries,
            client=client,
        ),
        client,
    )


def _ok_payload() -> dict[str, object]:
    return {
        "query": "acme widgets",
        "results": [
            {
                "title": "Acme Corp",
                "url": "https://www.acme.com/",
                "content": "Acme builds widgets.",
                "score": 0.91,
                "id": "result-1",
            }
        ],
        "response_time": 0.42,
    }


def _body(request: httpx.Request) -> dict[str, object]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


def _noop_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(
        "app.services.providers.tavily.time.sleep", delays.append
    )
    return delays


def test_request_is_constructed_from_documented_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok_payload())

    provider, _ = _provider(handler)
    provider.search("acme widgets", 7)

    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == TAVILY_SEARCH_URL
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Accept"] == "application/json"

    body = _body(request)
    assert body["query"] == "acme widgets"
    assert body["max_results"] == 7
    assert body["search_depth"] == "basic"
    assert body["include_answer"] is False
    assert body["include_raw_content"] is False
    assert body["include_images"] is False


def test_successful_response_is_normalized() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, json=_ok_payload()))

    [item] = provider.search("acme", 5)

    assert isinstance(item, SearchResultItem)
    assert item.title == "Acme Corp"
    assert str(item.url) == "https://www.acme.com/"
    assert item.snippet == "Acme builds widgets."
    assert item.provider == "tavily"
    assert item.provider_result_id == "result-1"


def test_multiple_results_keep_order_and_map_optional_fields() -> None:
    payload = {
        "query": "companies",
        "results": [
            {
                "title": "Alpha",
                "url": "https://alpha.example",
                "content": "Alpha snippet",
                "score": 0.9,
                "id": "a-1",
            },
            {
                "title": "Beta",
                "url": "https://beta.example",
                "content": None,
                "score": 0.5,
            },
        ],
    }
    provider, _ = _provider(lambda _: httpx.Response(200, json=payload))

    items = provider.search("companies", 5)

    assert [item.title for item in items] == ["Alpha", "Beta"]
    assert items[0].provider_result_id == "a-1"
    assert items[1].snippet is None
    assert items[1].provider_result_id is None


def test_empty_results_return_empty_list() -> None:
    provider, _ = _provider(
        lambda _: httpx.Response(200, json={"results": []})
    )
    assert provider.search("nothing", 5) == []


def test_malformed_results_are_skipped_individually() -> None:
    payload = {
        "results": [
            "not-an-object",
            {"title": "No URL", "content": "x"},
            {"url": "https://missing-title.com"},
            {"title": "   ", "url": "https://blank-title.com"},
            {"title": "FTP URL", "url": "ftp://files.example.com"},
            {
                "title": "Good",
                "url": "https://good.com",
                "content": None,
                "id": "good-1",
            },
        ]
    }
    provider, _ = _provider(lambda _: httpx.Response(200, json=payload))

    [item] = provider.search("q", 5)
    assert item.title == "Good"
    assert item.provider_result_id == "good-1"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": None},
        {"results": "not-a-list"},
        {"results": 3},
    ],
)
def test_missing_or_wrong_results_field_is_provider_unavailable(
    payload: dict[str, object],
) -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_non_json_body_is_provider_unavailable() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


def test_wrong_payload_shape_is_provider_unavailable() -> None:
    provider, _ = _provider(
        lambda _: httpx.Response(200, json=["not", "an", "object"])
    )
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_is_not_retried(status: int) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"detail": {"error": "bad key"}})

    provider, _ = _provider(handler, max_retries=3)
    with pytest.raises(ProviderAuthenticationError):
        provider.search("q", 5)
    assert len(calls) == 1


def test_server_error_is_retried_then_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _noop_sleep(monkeypatch)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"detail": {"error": "boom"}})

    provider, _ = _provider(handler, max_retries=2)
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)
    assert len(calls) == 3


def test_transient_server_error_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _noop_sleep(monkeypatch)
    responses = [httpx.Response(503), httpx.Response(200, json=_ok_payload())]

    provider, _ = _provider(lambda _: responses.pop(0), max_retries=1)
    assert len(provider.search("q", 5)) == 1


@pytest.mark.parametrize("status", [400, 432, 433])
def test_permanent_client_errors_are_not_retried(status: int) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"detail": {"error": "rejected"}})

    provider, _ = _provider(handler, max_retries=3)
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)
    assert len(calls) == 1


def test_rate_limit_is_retried_then_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _noop_sleep(monkeypatch)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"detail": {"error": "slow down"}})

    provider, _ = _provider(handler, max_retries=2)
    with pytest.raises(ProviderRateLimitError):
        provider.search("q", 5)
    assert len(calls) == 3
    assert delays == [0.25, 0.5]


def test_retry_after_header_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _noop_sleep(monkeypatch)
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json=_ok_payload()),
    ]

    provider, _ = _provider(lambda _: responses.pop(0), max_retries=1)
    assert len(provider.search("q", 5)) == 1
    assert delays == [2.0]


def test_retry_after_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _noop_sleep(monkeypatch)
    responses = [
        httpx.Response(503, headers={"Retry-After": "999"}),
        httpx.Response(200, json=_ok_payload()),
    ]

    provider, _ = _provider(lambda _: responses.pop(0), max_retries=1)
    assert len(provider.search("q", 5)) == 1
    assert delays == [5.0]


def test_invalid_retry_after_falls_back_to_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _noop_sleep(monkeypatch)
    responses = [
        httpx.Response(429, headers={"Retry-After": "not-seconds"}),
        httpx.Response(200, json=_ok_payload()),
    ]

    provider, _ = _provider(lambda _: responses.pop(0), max_retries=1)
    assert len(provider.search("q", 5)) == 1
    assert delays == [0.25]


def test_timeout_is_retried_then_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _noop_sleep(monkeypatch)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    provider, _ = _provider(handler, max_retries=1)
    with pytest.raises(ProviderTimeoutError):
        provider.search("q", 5)
    assert len(calls) == 2


def test_transport_error_is_retried_then_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _noop_sleep(monkeypatch)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(handler, max_retries=1)
    with pytest.raises(ProviderUnavailableError):
        provider.search("q", 5)
    assert len(calls) == 2


def test_errors_and_logs_never_include_the_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "tvly-super-secret-key"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TavilySearchProvider(secret, client=client)

    with caplog.at_level("DEBUG"):
        with pytest.raises(ProviderAuthenticationError) as excinfo:
            provider.search("q", 5)

    assert secret not in str(excinfo.value)
    assert secret not in caplog.text


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_missing_key_fails_at_search_without_a_request(
    api_key: str | None,
) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_ok_payload())

    provider, _ = _provider(handler, api_key=api_key)
    with pytest.raises(ProviderConfigurationError) as excinfo:
        provider.search("q", 5)

    assert calls == []
    assert "TAVILY_API_KEY" in str(excinfo.value)


def test_limit_is_bounded_and_empty_query_rejected() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    provider, _ = _provider(handler)
    provider.search("q", 999)
    assert _body(captured[0])["max_results"] == 20

    provider.search("q", 0)
    assert _body(captured[1])["max_results"] == 1

    with pytest.raises(ValueError):
        provider.search("   ", 5)


def test_unusually_long_provider_id_is_dropped() -> None:
    payload = {
        "results": [
            {
                "title": "Acme",
                "url": "https://acme.com",
                "content": "Widgets.",
                "id": "x" * 300,
            }
        ]
    }
    provider, _ = _provider(lambda _: httpx.Response(200, json=payload))

    [item] = provider.search("q", 5)
    assert item.provider_result_id is None


# -- factory ---------------------------------------------------------------


def test_factory_defaults_to_tavily_without_requiring_a_key() -> None:
    settings = Settings(_env_file=None)
    assert settings.search_provider == "tavily"

    provider = build_search_provider(settings)
    assert isinstance(provider, TavilySearchProvider)
    with pytest.raises(ProviderConfigurationError):
        provider.search("q", 5)
    provider.close()


def test_factory_returns_fake_when_selected() -> None:
    settings = Settings(_env_file=None, search_provider="fake")
    assert isinstance(build_search_provider(settings), FakeSearchProvider)


def test_factory_builds_tavily_with_key() -> None:
    settings = Settings(
        _env_file=None,
        search_provider="tavily",
        tavily_api_key="tvly-test-key",
    )
    provider = build_search_provider(settings)
    assert isinstance(provider, TavilySearchProvider)
    provider.close()


def test_fake_provider_respects_limit() -> None:
    results = [
        SearchResultItem(title=f"Co {i}", url=f"https://co{i}.com", provider="fake")
        for i in range(3)
    ]
    provider = FakeSearchProvider(results=results)
    assert len(provider.search("q", 2)) == 2
