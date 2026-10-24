"""Unit tests for the HTTPX website fetcher.

All HTTP is served by :class:`httpx.MockTransport` and DNS by an injected
resolver; no live website or DNS server is contacted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.services.providers import (
    FakeWebsiteFetcher,
    HttpxWebsiteFetcher,
    WebsiteFetcher,
    build_website_fetcher,
)
from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult
from app.services.providers.website_httpx import USER_AGENT

Handler = Callable[[httpx.Request], httpx.Response]

PUBLIC_V4 = "93.184.216.34"
OTHER_PUBLIC_V4 = "93.184.216.35"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
FIXED_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
FIXTURE_HTML = (
    Path(__file__).resolve().parent.parent / "fixtures" / "websites" / "acme_home.html"
).read_bytes()


class RecordingResolver:
    """Resolver stub that serves a static table and records lookups."""

    def __init__(self, table: dict[str, list[str]] | None = None) -> None:
        self.table = table or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return self.table.get(host, [PUBLIC_V4])


def _fetcher(
    handler: Handler,
    *,
    resolver: Callable[[str, int], list[str]] | None = None,
    **options: object,
) -> HttpxWebsiteFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpxWebsiteFetcher(
        client=client,
        resolver=resolver or RecordingResolver(),
        clock=lambda: FIXED_NOW,
        **options,  # type: ignore[arg-type]
    )


def _html(body: bytes = FIXTURE_HTML, *, content_type: str = "text/html; charset=utf-8", status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": content_type}, content=body)


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"No request expected, got {request.url}")


# -- protocol and construction --------------------------------------------
def test_adapters_satisfy_protocol() -> None:
    assert isinstance(FakeWebsiteFetcher(), WebsiteFetcher)
    with HttpxWebsiteFetcher() as fetcher:
        assert isinstance(fetcher, WebsiteFetcher)


def test_owned_client_ignores_proxy_env_and_redirects() -> None:
    fetcher = HttpxWebsiteFetcher()
    client = fetcher._client
    assert client.trust_env is False
    assert client.follow_redirects is False
    fetcher.close()
    assert client.is_closed


def test_injected_client_is_not_closed() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_never_called))
    HttpxWebsiteFetcher(client=client).close()
    assert not client.is_closed
    client.close()


@pytest.mark.parametrize(("name", "kind"), [("fake", FakeWebsiteFetcher), ("httpx", HttpxWebsiteFetcher)])
def test_builder_selects_configured_fetcher(name: str, kind: type) -> None:
    fetcher = build_website_fetcher(Settings(_env_file=None, website_fetcher=name))
    assert isinstance(fetcher, kind)
    close = getattr(fetcher, "close", None)
    if callable(close):
        close()


# -- successful fetches -----------------------------------------------------
def test_successful_html_fetch_extracts_title_and_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _html()

    result = _fetcher(handler).fetch("https://acme.com")

    assert result.outcome is FetchOutcome.SUCCESS
    assert result.failure is None
    assert result.requested_url == "https://acme.com"
    assert result.final_url == "https://acme.com/"
    assert result.http_status == 200
    assert result.content_type == "text/html; charset=utf-8"
    assert result.page_title == "Acme Widgets — Industrial Widgets for Teams"
    assert result.text is not None and "Widgets that ship" in result.text
    assert "SCRIPT-LEAK" not in result.text
    assert result.content_truncated is False
    assert result.fetched_at == FIXED_NOW
    assert result.fetcher == "httpx"
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].headers["user-agent"] == USER_AGENT


def test_connection_is_pinned_to_validated_ip_with_original_host() -> None:
    """DNS rebinding mitigation: connect to the validated IP, not the name."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _html()

    resolver = RecordingResolver()
    _fetcher(handler, resolver=resolver).fetch("https://acme.com/about?x=1")

    (request,) = seen
    assert request.url.host == PUBLIC_V4
    assert request.url.raw_path == b"/about?x=1"
    assert request.headers["host"] == "acme.com"
    assert request.extensions["sni_hostname"] == "acme.com"
    assert resolver.calls == [("acme.com", 443)]


def test_ipv6_resolution_is_pinned_with_brackets() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _html()

    resolver = RecordingResolver({"v6.example.com": [PUBLIC_V6]})
    result = _fetcher(handler, resolver=resolver).fetch("http://v6.example.com/")

    assert result.outcome is FetchOutcome.SUCCESS
    assert seen[0].url.host == PUBLIC_V6
    assert seen[0].headers["host"] == "v6.example.com"
    assert "sni_hostname" not in seen[0].extensions  # plain HTTP


def test_ipv4_addresses_are_tried_first() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return _html()

    resolver = RecordingResolver({"dual.example.com": [PUBLIC_V6, PUBLIC_V4]})
    _fetcher(handler, resolver=resolver).fetch("https://dual.example.com/")
    assert seen == [PUBLIC_V4]


@pytest.mark.parametrize("error", [httpx.ConnectError("unreachable"), httpx.ConnectTimeout("slow")])
def test_connect_failure_falls_back_to_next_validated_address(error: httpx.HTTPError) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == PUBLIC_V4:
            raise error
        return _html()

    resolver = RecordingResolver({"dual.example.com": [PUBLIC_V6, PUBLIC_V4]})
    result = _fetcher(handler, resolver=resolver).fetch("https://dual.example.com/")

    assert result.outcome is FetchOutcome.SUCCESS
    assert seen == [PUBLIC_V4, PUBLIC_V6]
    assert resolver.calls == [("dual.example.com", 443)]


def test_read_failure_does_not_retry_other_addresses() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        raise httpx.ReadTimeout("slow body")

    resolver = RecordingResolver({"dual.example.com": [PUBLIC_V4, OTHER_PUBLIC_V4]})
    result = _fetcher(handler, resolver=resolver).fetch("https://dual.example.com/")

    assert result.failure is FetchFailure.TIMEOUT
    assert seen == [PUBLIC_V4]


def test_all_addresses_failing_reports_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    resolver = RecordingResolver({"down.example.com": [PUBLIC_V4, OTHER_PUBLIC_V4]})
    result = _fetcher(handler, resolver=resolver).fetch("https://down.example.com/")
    assert result.failure is FetchFailure.CONNECTION_FAILED


def test_rebinding_resolver_cannot_change_destination_within_a_hop() -> None:
    """A resolver that answers public first and private later is used once."""
    answers = iter([[PUBLIC_V4], ["127.0.0.1"]])
    calls: list[str] = []

    def rebinding(host: str, port: int) -> list[str]:
        calls.append(host)
        return next(answers)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return _html()

    result = _fetcher(handler, resolver=rebinding).fetch("https://rebind.example.com/")

    assert result.outcome is FetchOutcome.SUCCESS
    assert calls == ["rebind.example.com"]
    assert seen == [PUBLIC_V4]


def test_charset_from_header_is_used() -> None:
    body = "<title>Café</title><p>Müller GmbH</p>".encode("latin-1")
    result = _fetcher(
        lambda r: _html(body, content_type="text/html; charset=iso-8859-1")
    ).fetch("https://acme.com/")
    assert result.page_title == "Café"
    assert result.text == "Müller GmbH"


def test_xhtml_content_type_is_accepted() -> None:
    result = _fetcher(
        lambda r: _html(b"<p>xhtml</p>", content_type="application/xhtml+xml")
    ).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.SUCCESS


# -- empty, partial, malformed ------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [b"", b"<html><head><title>Only title</title></head><body></body></html>", b"<script>app()</script>"],
)
def test_empty_pages_are_reported_honestly(body: bytes) -> None:
    result = _fetcher(lambda r: _html(body)).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.EMPTY
    assert result.text is None
    assert result.failure is None
    assert not result.usable


def test_malformed_html_is_parsed_best_effort() -> None:
    result = _fetcher(
        lambda r: _html(b"<html><body><p>Acme <b>widgets<div></span>")
    ).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.SUCCESS
    assert result.text is not None and "Acme" in result.text


def test_oversized_response_is_truncated_and_partial() -> None:
    body = b"<p>" + b"widget " * 2000 + b"</p>"
    result = _fetcher(
        lambda r: _html(body), max_response_bytes=1000
    ).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.PARTIAL
    assert result.content_truncated is True
    assert result.text is not None and len(result.text) <= 1000


def test_streamed_response_stops_reading_at_limit() -> None:
    produced: list[int] = []

    def chunks():  # type: ignore[no-untyped-def]
        for index in range(1000):
            produced.append(index)
            yield b"<p>chunk</p>" * 10

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=chunks())

    result = _fetcher(handler, max_response_bytes=500).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.PARTIAL
    assert len(produced) < 10


def test_extracted_text_cap_marks_partial() -> None:
    body = b"<p>" + b"word " * 500 + b"</p>"
    result = _fetcher(lambda r: _html(body), max_text_chars=50).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.PARTIAL
    assert result.text is not None and len(result.text) <= 50


# -- HTTP and content failures --------------------------------------------------
@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/json", "text/plain", "application/octet-stream"],
)
def test_unsupported_content_types_are_rejected(content_type: str) -> None:
    result = _fetcher(
        lambda r: httpx.Response(200, headers={"Content-Type": content_type}, content=b"%PDF")
    ).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.FAILED
    assert result.failure is FetchFailure.UNSUPPORTED_CONTENT_TYPE
    assert result.content_type == content_type
    assert result.text is None


def test_missing_content_type_is_rejected() -> None:
    result = _fetcher(lambda r: httpx.Response(200, content=b"<p>x</p>")).fetch("https://acme.com/")
    assert result.failure is FetchFailure.UNSUPPORTED_CONTENT_TYPE


@pytest.mark.parametrize("status", [400, 403, 404, 410, 429, 500, 503])
def test_http_errors_are_reported_with_status(status: int) -> None:
    result = _fetcher(lambda r: _html(b"<p>error page</p>", status=status)).fetch(
        "https://acme.com/"
    )
    assert result.outcome is FetchOutcome.FAILED
    assert result.failure is FetchFailure.HTTP_ERROR
    assert result.http_status == status
    assert result.text is None


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (httpx.ConnectTimeout("connect timed out"), FetchFailure.TIMEOUT),
        (httpx.ReadTimeout("read timed out"), FetchFailure.TIMEOUT),
        (httpx.PoolTimeout("pool timed out"), FetchFailure.TIMEOUT),
        (httpx.ConnectError("refused"), FetchFailure.CONNECTION_FAILED),
        (httpx.ReadError("reset"), FetchFailure.CONNECTION_FAILED),
        (httpx.RemoteProtocolError("bad framing"), FetchFailure.MALFORMED_RESPONSE),
        (httpx.DecodingError("bad gzip"), FetchFailure.MALFORMED_RESPONSE),
    ],
)
def test_transport_failures_are_categorized(
    error: httpx.HTTPError, failure: FetchFailure
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    result = _fetcher(handler).fetch("https://acme.com/")
    assert result.outcome is FetchOutcome.FAILED
    assert result.failure is failure
    assert result.final_url == "https://acme.com/"
    assert result.http_status is None


def test_total_deadline_bounds_processing_time() -> None:
    ticks = iter([0.0, 100.0, 200.0, 300.0])
    result = _fetcher(
        _never_called, max_fetch_seconds=5.0, monotonic=lambda: next(ticks)
    ).fetch("https://acme.com/")
    assert result.failure is FetchFailure.TIMEOUT


# -- redirects -------------------------------------------------------------------
def test_relative_redirect_is_followed_and_final_url_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"Location": "/en/home"})
        return _html()

    resolver = RecordingResolver()
    result = _fetcher(handler, resolver=resolver).fetch("https://acme.com")

    assert result.outcome is FetchOutcome.SUCCESS
    assert result.requested_url == "https://acme.com"
    assert result.final_url == "https://acme.com/en/home"
    # Every hop is resolved and validated again.
    assert resolver.calls == [("acme.com", 443), ("acme.com", 443)]


def test_cross_host_redirect_is_revalidated_and_pinned() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        if request.headers["host"] == "acme.com":
            return httpx.Response(302, headers={"Location": "https://www.acme-group.com/"})
        return _html()

    resolver = RecordingResolver({"www.acme-group.com": [OTHER_PUBLIC_V4]})
    result = _fetcher(handler, resolver=resolver).fetch("http://acme.com/")

    assert result.final_url == "https://www.acme-group.com/"
    assert seen == [(PUBLIC_V4, "acme.com"), (OTHER_PUBLIC_V4, "www.acme-group.com")]


def test_redirect_limit_is_enforced() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(302, headers={"Location": f"/hop{len(calls)}"})

    result = _fetcher(handler, max_redirects=3).fetch("https://acme.com/")
    assert result.failure is FetchFailure.TOO_MANY_REDIRECTS
    assert len(calls) == 4
    assert result.http_status == 302


def test_zero_redirects_allowed() -> None:
    result = _fetcher(
        lambda r: httpx.Response(301, headers={"Location": "/x"}), max_redirects=0
    ).fetch("https://acme.com/")
    assert result.failure is FetchFailure.TOO_MANY_REDIRECTS


def test_redirect_without_location_is_invalid() -> None:
    result = _fetcher(lambda r: httpx.Response(302)).fetch("https://acme.com/")
    assert result.failure is FetchFailure.INVALID_REDIRECT


def test_http_error_after_redirect_keeps_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"Location": "/gone"})
        return _html(status=404)

    result = _fetcher(handler).fetch("https://acme.com/")
    assert result.failure is FetchFailure.HTTP_ERROR
    assert result.final_url == "https://acme.com/gone"


@pytest.mark.parametrize(
    ("location", "failure"),
    [
        ("http://127.0.0.1/admin", FetchFailure.BLOCKED_DESTINATION),
        ("http://localhost:80/", FetchFailure.BLOCKED_DESTINATION),
        ("http://169.254.169.254/latest/meta-data/", FetchFailure.BLOCKED_DESTINATION),
        ("http://[::1]/", FetchFailure.BLOCKED_DESTINATION),
        ("http://[fd00:ec2::254]/", FetchFailure.BLOCKED_DESTINATION),
        ("http://10.0.0.1/", FetchFailure.BLOCKED_DESTINATION),
        ("http://2130706433/", FetchFailure.INVALID_URL),
        ("http://metadata.google.internal/", FetchFailure.BLOCKED_DESTINATION),
        ("https://internal.example.com/", FetchFailure.BLOCKED_DESTINATION),
        ("file:///etc/passwd", FetchFailure.DISALLOWED_SCHEME),
        ("gopher://acme.com/", FetchFailure.DISALLOWED_SCHEME),
        ("https://user:pw@acme.com/", FetchFailure.CREDENTIALS_IN_URL),
        ("https://acme.com:6379/", FetchFailure.DISALLOWED_PORT),
    ],
)
def test_unsafe_redirect_targets_are_not_followed(
    location: str, failure: FetchFailure
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})

    resolver = RecordingResolver({"internal.example.com": ["192.168.0.10"]})
    result = _fetcher(handler, resolver=resolver).fetch("https://acme.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure is failure
    assert len(seen) == 1
    assert result.final_url == "https://acme.com/"


def test_redirect_to_same_host_that_rebinds_is_blocked() -> None:
    """Each hop resolves again, so a rebind between hops is caught."""
    answers = iter([[PUBLIC_V4], ["10.0.0.7"]])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "/next"})

    result = _fetcher(handler, resolver=lambda h, p: next(answers)).fetch(
        "https://rebind.example.com/"
    )
    assert result.failure is FetchFailure.BLOCKED_DESTINATION
    assert seen == [PUBLIC_V4]


# -- rejected before any request --------------------------------------------------
@pytest.mark.parametrize(
    ("url", "failure"),
    [
        ("not a url", FetchFailure.INVALID_URL),
        ("ftp://acme.com/", FetchFailure.DISALLOWED_SCHEME),
        ("https://admin:secret@acme.com/", FetchFailure.CREDENTIALS_IN_URL),
        ("https://acme.com:8080/", FetchFailure.DISALLOWED_PORT),
        ("https://acme.com:70000/", FetchFailure.INVALID_URL),
        ("http://127.0.0.1/", FetchFailure.BLOCKED_DESTINATION),
        ("http://[::1]/", FetchFailure.BLOCKED_DESTINATION),
        ("http://169.254.169.254/", FetchFailure.BLOCKED_DESTINATION),
        ("http://0x7f.1/", FetchFailure.INVALID_URL),
        ("http://localhost/", FetchFailure.BLOCKED_DESTINATION),
    ],
)
def test_invalid_urls_make_no_request_or_lookup(url: str, failure: FetchFailure) -> None:
    resolver = RecordingResolver()
    result = _fetcher(_never_called, resolver=resolver).fetch(url)
    assert result.outcome is FetchOutcome.FAILED
    assert result.failure is failure
    assert result.final_url is None
    assert result.requested_url == url
    assert resolver.calls == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.20.30.40",  # private
        "172.20.0.1",  # private
        "192.168.10.1",  # private
        "169.254.169.254",  # link-local / cloud metadata
        "224.0.0.251",  # multicast
        "240.0.0.1",  # reserved
        "0.0.0.0",  # unspecified
        "100.100.100.200",  # shared address space (Alibaba metadata)
        "::1",
        "fe80::1",
        "fd00:ec2::254",
        "ff02::1",
        "::ffff:10.0.0.1",
    ],
)
def test_hostnames_resolving_to_non_public_addresses_are_blocked(address: str) -> None:
    resolver = RecordingResolver({"evil.example.com": [address]})
    result = _fetcher(_never_called, resolver=resolver).fetch("https://evil.example.com/")
    assert result.failure is FetchFailure.BLOCKED_DESTINATION
    assert result.final_url is None


def test_dns_failure_is_categorized_without_request() -> None:
    def resolver(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    result = _fetcher(_never_called, resolver=resolver).fetch("https://nowhere.example.com/")
    assert result.failure is FetchFailure.DNS_RESOLUTION_FAILED


# -- fake fetcher ------------------------------------------------------------------
def test_fake_fetcher_is_deterministic_and_records_calls() -> None:
    preset = FetchResult(
        requested_url="ignored",
        fetched_at=FIXED_NOW,
        outcome=FetchOutcome.SUCCESS,
        fetcher="ignored",
        final_url="https://acme.com/",
        text="Acme",
    )
    fake = FakeWebsiteFetcher(results={"https://acme.com/": preset})

    first = fake.fetch("https://acme.com/")
    second = fake.fetch("https://acme.com/")
    unknown = fake.fetch("https://other.com/")

    assert first == second
    assert first.requested_url == "https://acme.com/"
    assert first.fetcher == "fake"
    assert unknown.outcome is FetchOutcome.FAILED
    assert unknown.failure is FetchFailure.CONNECTION_FAILED
    assert unknown.text is None
    assert fake.calls == ["https://acme.com/", "https://acme.com/", "https://other.com/"]
