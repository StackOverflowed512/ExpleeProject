"""Unit tests for the HTTPX website fetcher.

All requests are served by ``httpx.MockTransport`` and DNS is injected, so no
test performs network access. Failure paths return typed results rather than
raising.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

import httpx
import pytest

from app.services.providers.http_website import HttpWebsiteFetcher
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
)

Handler = Callable[[httpx.Request], httpx.Response]
FIXED_NOW = datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc)
PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"


def _fetcher(
    handler: Handler,
    *,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    **kwargs: object,
) -> HttpWebsiteFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpWebsiteFetcher(
        client=client,
        resolver=resolver or (lambda host, port: [PUBLIC_IP]),
        clock=lambda: FIXED_NOW,
        **kwargs,
    )


def _html_handler(html: str, *, content_type: str = "text/html"):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": content_type},
            text=html,
        )

    return handler


HTML_PAGE = """
<!doctype html>
<html>
  <head>
    <title>  Acme   Corp  </title>
    <style>.hidden { display: none }</style>
    <script>window.secret = "script-secret";</script>
  </head>
  <body>
    <header>Header menu</header>
    <nav>Navigation menu</nav>
    <main>
      <h1>Hello</h1>
      <p>We build   reliable widgets.</p>
      <div role="navigation">Breadcrumbs</div>
      <div aria-hidden="true">Decorative</div>
      <form>Contact form</form>
    </main>
    <aside>Sidebar</aside>
    <footer>Footer text</footer>
  </body>
</html>
"""


def test_extracts_title_and_visible_text() -> None:
    fetcher = _fetcher(_html_handler(HTML_PAGE))

    result = fetcher.fetch("https://acme.com")

    assert result.outcome is FetchOutcome.SUCCESS
    assert result.requested_url == "https://acme.com"
    assert result.final_url == "https://acme.com/"
    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.page_title == "Acme Corp"
    assert result.fetched_at == FIXED_NOW
    assert result.failure_category is None
    assert result.truncated is False
    assert "Hello" in result.text
    assert "We build reliable widgets." in result.text
    for excluded in (
        "script-secret",
        "Header menu",
        "Navigation menu",
        "Breadcrumbs",
        "Decorative",
        "Contact form",
        "Sidebar",
        "Footer text",
        ".hidden",
    ):
        assert excluded not in result.text


def test_request_is_pinned_to_validated_ip_with_logical_host() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>ok</body></html>",
        )

    fetcher = _fetcher(handler)
    fetcher.fetch("https://acme.com/page")

    request = captured[0]
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/page"
    assert request.headers["host"] == "acme.com"
    assert request.extensions["sni_hostname"] == "acme.com"
    assert request.headers["accept-encoding"] == "identity"
    assert "ExpleeResearch" in request.headers["user-agent"]


def test_follows_redirects_and_reports_final_url() -> None:
    requested_hosts: list[str] = []

    def resolver(host: str, port: int) -> Sequence[str]:
        return [PUBLIC_IP] if host == "example.com" else [PUBLIC_IP_2]

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.headers["host"])
        if request.headers["host"] == "example.com":
            return httpx.Response(
                301, headers={"location": "https://www.example.org/final"}
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Final</title></head><body>Done</body></html>",
        )

    fetcher = _fetcher(handler, resolver=resolver)
    result = fetcher.fetch("https://example.com/start")

    assert result.outcome is FetchOutcome.SUCCESS
    assert result.final_url == "https://www.example.org/final"
    assert result.status_code == 200
    assert result.page_title == "Final"
    assert result.text == "Done"
    assert requested_hosts == ["example.com", "www.example.org"]


def test_relative_redirect_is_resolved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/about"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>About</body></html>",
        )

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.final_url == "https://example.com/about"
    assert result.text == "About"


def test_redirect_limit_is_enforced() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(302, headers={"location": "/again"})

    result = _fetcher(handler, max_redirects=1).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure_category is FetchFailureCategory.TOO_MANY_REDIRECTS
    assert result.status_code == 302
    assert len(calls) == 2


def test_redirect_loop_is_detected() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        target = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"location": target})

    result = _fetcher(handler, max_redirects=5).fetch(
        "https://example.com/a"
    )

    assert result.failure_category is FetchFailureCategory.REDIRECT_LOOP
    assert len(calls) == 2


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/",
        "http://[::1]/",
    ],
)
def test_unsafe_redirect_is_blocked_before_request(location: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure_category is FetchFailureCategory.BLOCKED_DESTINATION
    assert result.final_url == "https://example.com/"
    assert len(calls) == 1


def test_redirect_without_location_is_malformed() -> None:
    result = _fetcher(lambda _: httpx.Response(302)).fetch(
        "https://example.com/"
    )

    assert result.failure_category is FetchFailureCategory.MALFORMED_RESPONSE
    assert result.status_code == 302


@pytest.mark.parametrize("status", [400, 403, 404, 410, 500, 503])
def test_http_errors_do_not_become_content(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "text/html"},
            text="<html><title>Error</title><body>Error page</body></html>",
        )

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure_category is FetchFailureCategory.HTTP_ERROR
    assert result.status_code == status
    assert result.page_title is None
    assert result.text == ""


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/json", "text/plain", ""],
)
def test_unsupported_content_types_are_rejected(content_type: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, text="not html")

    result = _fetcher(handler).fetch("https://example.com/file")

    assert result.outcome is FetchOutcome.FAILED
    assert (
        result.failure_category
        is FetchFailureCategory.UNSUPPORTED_CONTENT_TYPE
    )


def test_xhtml_is_supported() -> None:
    fetcher = _fetcher(
        _html_handler(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>XHTML</body></html>',
            content_type="application/xhtml+xml; charset=utf-8",
        )
    )

    result = fetcher.fetch("https://example.com/")

    assert result.outcome is FetchOutcome.SUCCESS
    assert result.text == "XHTML"


def test_timeout_is_a_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure_category is FetchFailureCategory.TIMEOUT
    assert result.final_url == "https://example.com/"


def test_connection_error_is_a_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.failure_category is FetchFailureCategory.CONNECTION_ERROR


def test_dns_failure_never_calls_the_transport() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    def resolver(host: str, port: int) -> Sequence[str]:
        raise socket.gaierror("no such host")

    result = _fetcher(handler, resolver=resolver).fetch(
        "https://missing.example/"
    )

    assert result.failure_category is FetchFailureCategory.DNS_FAILURE
    assert calls == []


def test_declared_oversized_response_is_rejected_before_reading() -> None:
    body = b"<html><body>" + (b"x" * 5000) + b"</body></html>"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-length": str(len(body)),
            },
            content=body,
        )

    result = _fetcher(handler, max_bytes=100).fetch("https://example.com/")

    assert result.failure_category is FetchFailureCategory.RESPONSE_TOO_LARGE
    assert result.text == ""


def test_streamed_oversized_response_is_capped() -> None:
    payload = b"x" * 101

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=httpx.ByteStream(payload),
        )

    result = _fetcher(handler, max_bytes=100).fetch("https://example.com/")

    assert result.failure_category is FetchFailureCategory.RESPONSE_TOO_LARGE


def test_text_is_truncated_and_marked_partial() -> None:
    html = "<html><body><main>" + ("word " * 100) + "</main></body></html>"

    result = _fetcher(_html_handler(html), max_text_chars=20).fetch(
        "https://example.com/"
    )

    assert result.outcome is FetchOutcome.PARTIAL
    assert result.truncated is True
    assert len(result.text) == 20


def test_empty_html_is_partial_without_invented_facts() -> None:
    result = _fetcher(
        _html_handler("<html><head></head><body></body></html>")
    ).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.PARTIAL
    assert result.page_title is None
    assert result.text == ""
    assert result.truncated is False


def test_malformed_html_is_parsed_leniently() -> None:
    result = _fetcher(
        _html_handler("<html><body><p>unclosed <b>markup")
    ).fetch("https://example.com/")

    assert result.outcome is FetchOutcome.SUCCESS
    assert "unclosed" in result.text
    assert "markup" in result.text


def test_charset_is_honoured() -> None:
    body = "<html><body>Café</body></html>".encode("iso-8859-1")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=iso-8859-1"},
            content=body,
        )

    result = _fetcher(handler).fetch("https://example.com/")

    assert result.text == "Café"


def test_invalid_url_never_calls_transport_or_resolver() -> None:
    calls: list[int] = []
    resolver_calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    def resolver(host: str, port: int) -> Sequence[str]:
        resolver_calls.append(1)
        return [PUBLIC_IP]

    result = _fetcher(handler, resolver=resolver).fetch(
        "http://user:secret@example.com/"
    )

    assert result.failure_category is FetchFailureCategory.INVALID_URL
    assert result.final_url is None
    assert calls == []
    assert resolver_calls == []


def test_initial_private_destination_never_calls_transport() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    result = _fetcher(handler).fetch("http://127.0.0.1/")

    assert result.failure_category is FetchFailureCategory.BLOCKED_DESTINATION
    assert calls == []


def test_logs_do_not_include_page_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_marker = "SECRET-PAGE-CONTENT-" + ("z" * 5000)
    html = f"<html><body>{secret_marker}</body></html>"

    with caplog.at_level(logging.INFO):
        _fetcher(_html_handler(html)).fetch("https://example.com/")

    assert secret_marker not in caplog.text
