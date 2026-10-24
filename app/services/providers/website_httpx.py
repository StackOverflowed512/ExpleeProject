"""HTTPX-backed website fetcher with SSRF protections.

Every hop (the initial URL and each redirect target) goes through the same
sequence:

1. :func:`check_url` validates the URL syntax, scheme, port, and host form.
2. The host is resolved once and every resolved address must be public.
   Resolution runs on a small worker pool and is abandoned after
   ``connect_timeout_seconds`` or the remaining total budget, whichever is
   shorter, so a stalled resolver cannot exceed the fetch deadline.
3. The request is sent to a validated IP address itself (DNS pinning),
   IPv4 addresses first; only a connect failure moves to the next one.
   The original hostname is kept in the ``Host`` header and as the TLS
   server name (``sni_hostname``), so certificates are still verified
   against the hostname. The HTTP client never resolves the name again,
   which closes the window between validation and connection that a DNS
   rebinding attack relies on.

Redirects are followed manually up to a limit, and each target is
revalidated from step 1. The client ignores proxy environment variables
(a proxy would resolve hostnames itself) and does not keep connections
alive, so one connection is never reused for a different hostname.

Responses are streamed: reading stops at ``max_response_bytes``, and a
total wall-clock deadline bounds slow responses. Only HTML content types
are parsed. The adapter never logs URLs with query strings, bodies, or
page content.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from app.services.providers.html_text import extract_text
from app.services.providers.url_safety import (
    DEFAULT_PORTS,
    IPAddress,
    Resolver,
    UnsafeUrlError,
    ValidatedUrl,
    check_url,
    resolve_public_addresses,
    system_resolver,
    validate_resolved_addresses,
)
from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult

logger = logging.getLogger(__name__)

FETCHER_NAME = "httpx"
USER_AGENT = "ExpleeResearch/0.1 (+company website research)"
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_CONTENT_TYPE_LENGTH = 255
MAX_ADDRESSES_PER_HOP = 4
# Concurrent DNS lookups per fetcher. A lookup that outlives its timeout
# keeps its worker until the OS resolver returns; when every worker is
# stuck, new lookups queue and time out, so the fetcher fails closed.
DNS_WORKERS = 8


@dataclass
class _HopState:
    """Metadata of the most recent request, kept for failure reporting."""

    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None


class _FetchFailed(Exception):
    def __init__(self, failure: FetchFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


def _connect_url(target: ValidatedUrl, address: IPAddress) -> str:
    ip_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if target.port != DEFAULT_PORTS[target.scheme]:
        ip_host = f"{ip_host}:{target.port}"
    return f"{target.scheme}://{ip_host}{target.path_and_query}"


def _host_header(target: ValidatedUrl) -> str:
    host = f"[{target.host}]" if ":" in target.host else target.host
    if target.port != DEFAULT_PORTS[target.scheme]:
        host = f"{host}:{target.port}"
    return host


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


class HttpxWebsiteFetcher:
    """Fetch public company web pages with bounded, validated requests."""

    name = FETCHER_NAME

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 10.0,
        max_fetch_seconds: float = 20.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 5,
        max_text_chars: int = 50_000,
        resolver: Resolver = system_resolver,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_fetch_seconds = max_fetch_seconds
        self._dns_timeout = connect_timeout_seconds
        self._dns_pool = ThreadPoolExecutor(
            max_workers=DNS_WORKERS, thread_name_prefix="explee-dns"
        )
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max(0, max_redirects)
        self._max_text_chars = max_text_chars
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
        )
        self._owns_client = client is None

    def close(self) -> None:
        # Do not wait for lookups stuck in the OS resolver.
        self._dns_pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpxWebsiteFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch(self, url: str) -> FetchResult:
        fetched_at = self._clock()
        deadline = self._monotonic() + self._max_fetch_seconds
        state = _HopState()

        def failed(failure: FetchFailure) -> FetchResult:
            logger.debug("Website fetch failed: %s", failure.value)
            return FetchResult(
                requested_url=url,
                fetched_at=fetched_at,
                outcome=FetchOutcome.FAILED,
                fetcher=self.name,
                final_url=state.final_url,
                http_status=state.http_status,
                content_type=state.content_type,
                failure=failure,
            )

        try:
            return self._fetch(url, fetched_at, deadline, state)
        except UnsafeUrlError as exc:
            return failed(exc.failure)
        except _FetchFailed as exc:
            return failed(exc.failure)
        except httpx.TimeoutException:
            return failed(FetchFailure.TIMEOUT)
        except (httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.DecodingError):
            return failed(FetchFailure.MALFORMED_RESPONSE)
        except httpx.TransportError:
            return failed(FetchFailure.CONNECTION_FAILED)
        except httpx.InvalidURL:
            return failed(FetchFailure.INVALID_URL)
        except httpx.HTTPError:
            return failed(FetchFailure.MALFORMED_RESPONSE)

    # -- internals ------------------------------------------------------
    def _check_deadline(self, deadline: float) -> None:
        if self._monotonic() > deadline:
            raise _FetchFailed(FetchFailure.TIMEOUT)

    def _resolve(self, target: ValidatedUrl, deadline: float) -> list[IPAddress]:
        """Resolve ``target`` within the DNS timeout and remaining budget."""
        if target.is_ip_literal:
            return resolve_public_addresses(target, self._resolver)

        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _FetchFailed(FetchFailure.TIMEOUT)
        future = self._dns_pool.submit(self._resolver, target.host, target.port)
        try:
            raw_addresses = future.result(timeout=min(remaining, self._dns_timeout))
        except FutureTimeoutError:
            future.cancel()
            raise _FetchFailed(FetchFailure.TIMEOUT) from None
        except (OSError, UnicodeError):
            raise UnsafeUrlError(
                FetchFailure.DNS_RESOLUTION_FAILED,
                "The website host could not be resolved.",
            ) from None
        return validate_resolved_addresses(raw_addresses)

    def _send_pinned(
        self,
        target: ValidatedUrl,
        addresses: list[IPAddress],
        deadline: float,
    ) -> httpx.Response:
        """Send the request to the validated addresses, IPv4 first.

        Only connection-establishment failures move on to the next address,
        so a host whose IPv6 address is unreachable still works. Every
        candidate address was already validated as public.
        """
        ordered = sorted(addresses, key=lambda address: address.version)
        last_error: httpx.TransportError | None = None
        for address in ordered[:MAX_ADDRESSES_PER_HOP]:
            self._check_deadline(deadline)
            request = self._client.build_request(
                "GET",
                _connect_url(target, address),
                headers={
                    "Host": _host_header(target),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9",
                    "Accept-Encoding": "identity",
                },
                extensions=(
                    {"sni_hostname": target.host}
                    if target.scheme == "https" and not target.is_ip_literal
                    else {}
                ),
            )
            try:
                return self._client.send(request, stream=True, follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _fetch(
        self,
        url: str,
        fetched_at: datetime,
        deadline: float,
        state: _HopState,
    ) -> FetchResult:
        target = check_url(url)

        for hop in range(self._max_redirects + 1):
            self._check_deadline(deadline)
            addresses = self._resolve(target, deadline)
            state.final_url = target.url
            state.http_status = None
            state.content_type = None

            response = self._send_pinned(target, addresses, deadline)
            try:
                status = response.status_code
                state.http_status = status
                raw_type = response.headers.get("content-type")
                state.content_type = (
                    raw_type.strip()[:MAX_CONTENT_TYPE_LENGTH] if raw_type else None
                )

                if status in REDIRECT_STATUSES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise _FetchFailed(FetchFailure.INVALID_REDIRECT)
                    if hop >= self._max_redirects:
                        raise _FetchFailed(FetchFailure.TOO_MANY_REDIRECTS)
                    try:
                        next_url = urljoin(target.url, location)
                    except ValueError:
                        raise _FetchFailed(FetchFailure.INVALID_REDIRECT) from None
                    target = check_url(next_url)
                    continue

                if not 200 <= status < 300:
                    raise _FetchFailed(FetchFailure.HTTP_ERROR)
                if _media_type(raw_type) not in HTML_CONTENT_TYPES:
                    raise _FetchFailed(FetchFailure.UNSUPPORTED_CONTENT_TYPE)

                body, body_truncated = self._read_body(response, deadline)
                encoding = response.charset_encoding
            finally:
                response.close()

            return self._build_result(
                url, fetched_at, state, body, body_truncated, encoding
            )

        raise _FetchFailed(FetchFailure.TOO_MANY_REDIRECTS)  # pragma: no cover

    def _read_body(
        self, response: httpx.Response, deadline: float
    ) -> tuple[bytes, bool]:
        limit = self._max_response_bytes
        body = bytearray()
        for chunk in response.iter_bytes():
            remaining = limit - len(body)
            if len(chunk) > remaining:
                body.extend(chunk[:remaining])
                return bytes(body), True
            body.extend(chunk)
            self._check_deadline(deadline)
        return bytes(body), False

    def _build_result(
        self,
        url: str,
        fetched_at: datetime,
        state: _HopState,
        body: bytes,
        body_truncated: bool,
        encoding: str | None,
    ) -> FetchResult:
        try:
            extracted = extract_text(
                body, encoding=encoding, max_chars=self._max_text_chars
            )
        except Exception:  # noqa: BLE001 - parser defects must not escape
            raise _FetchFailed(FetchFailure.MALFORMED_RESPONSE) from None

        truncated = body_truncated or extracted.truncated
        if not extracted.text:
            outcome = FetchOutcome.EMPTY
        elif truncated:
            outcome = FetchOutcome.PARTIAL
        else:
            outcome = FetchOutcome.SUCCESS

        return FetchResult(
            requested_url=url,
            fetched_at=fetched_at,
            outcome=outcome,
            fetcher=self.name,
            final_url=state.final_url,
            http_status=state.http_status,
            content_type=state.content_type,
            page_title=extracted.title,
            text=extracted.text or None,
            content_truncated=truncated,
        )
