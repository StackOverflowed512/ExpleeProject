"""HTTPX-backed website fetcher with bounded resources and SSRF checks.

The adapter connects to a validated public IP address while preserving the
logical ``Host`` header and TLS SNI. Every redirect is resolved and validated
again before it is requested. Redirects are followed manually so the client
can never bypass those checks.

Only HTML and XHTML responses are processed. Scripts, styles, navigation,
forms, and similar boilerplate are removed before visible text is extracted.
Failure is a typed result, never an exception leaking response details.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.services.providers.url_safety import (
    AddressResolver,
    UnsafeUrlError,
    ValidatedTarget,
    validate_public_url,
)
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetchResult,
)

logger = logging.getLogger(__name__)

USER_AGENT = "ExpleeResearch/0.1"
MAX_TITLE_CHARS = 500
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_REMOVABLE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "dialog",
    "nav",
    "header",
    "footer",
    "aside",
)
_REMOVABLE_ROLES = ("navigation", "banner", "contentinfo", "complementary")

Clock = Callable[[], datetime]


class HttpWebsiteFetcher:
    """Fetch and extract one public company website page."""

    name = "http"

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 10.0,
        total_timeout_seconds: float = 15.0,
        max_bytes: int = 2_000_000,
        max_redirects: int = 3,
        max_text_chars: int = 20_000,
        client: httpx.Client | None = None,
        resolver: AddressResolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Website fetch timeouts must be positive.")
        if total_timeout_seconds <= 0:
            raise ValueError("The website fetch deadline must be positive.")
        if max_bytes <= 0 or max_text_chars <= 0:
            raise ValueError("Website fetch size limits must be positive.")
        if max_redirects < 0:
            raise ValueError("The redirect limit must not be negative.")

        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_text_chars = max_text_chars
        self._resolver = resolver
        self._clock = clock
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
        )
        self._owns_client = client is None

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpWebsiteFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- public contract ------------------------------------------------
    def fetch(self, url: str) -> WebsiteFetchResult:
        """Fetch ``url`` with redirects, size, and time limits."""
        requested_url = (url or "").strip()
        deadline = time.monotonic() + self._total_timeout
        current_url = requested_url
        last_final_url: str | None = None
        last_status: int | None = None
        last_content_type: str | None = None
        last_failure = FetchFailureCategory.CONNECTION_ERROR
        redirects = 0
        visited: set[str] = set()

        while True:
            try:
                target = validate_public_url(
                    current_url, resolver=self._resolver
                )
            except UnsafeUrlError as exc:
                self._log_failure(exc.category, last_final_url, None)
                return self._failure(
                    requested_url,
                    exc.category,
                    final_url=last_final_url,
                    status_code=last_status,
                    content_type=last_content_type,
                )

            if target.logical_url in visited:
                self._log_failure(
                    FetchFailureCategory.REDIRECT_LOOP,
                    last_final_url,
                    last_status,
                )
                return self._failure(
                    requested_url,
                    FetchFailureCategory.REDIRECT_LOOP,
                    final_url=last_final_url,
                    status_code=last_status,
                    content_type=last_content_type,
                )

            visited.add(target.logical_url)
            current_url = target.logical_url

            if time.monotonic() >= deadline:
                return self._failure(
                    requested_url,
                    FetchFailureCategory.TIMEOUT,
                    final_url=current_url,
                    status_code=last_status,
                    content_type=last_content_type,
                )

            response: httpx.Response | None = None
            for address in target.addresses:
                if time.monotonic() >= deadline:
                    last_failure = FetchFailureCategory.TIMEOUT
                    break
                request = self._build_request(target, address, deadline)
                try:
                    response = self._client.send(
                        request,
                        stream=True,
                        follow_redirects=False,
                    )
                    break
                except httpx.TimeoutException:
                    last_failure = FetchFailureCategory.TIMEOUT
                    break
                except Exception as exc:  # noqa: BLE001 - mapped to a typed result
                    last_failure = FetchFailureCategory.CONNECTION_ERROR
                    logger.debug(
                        "Website connection attempt failed: %s",
                        type(exc).__name__,
                    )
                    continue

            if response is None:
                self._log_failure(last_failure, current_url, last_status)
                return self._failure(
                    requested_url,
                    last_failure,
                    final_url=current_url,
                    status_code=last_status,
                    content_type=last_content_type,
                )

            try:
                status = response.status_code
                raw_content_type = response.headers.get("content-type")
                content_type = (
                    raw_content_type[:255] if raw_content_type else None
                )
                last_status = status
                last_content_type = content_type

                if 300 <= status < 400:
                    location = response.headers.get("location")
                    if not location:
                        self._log_failure(
                            FetchFailureCategory.MALFORMED_RESPONSE,
                            current_url,
                            status,
                        )
                        return self._failure(
                            requested_url,
                            FetchFailureCategory.MALFORMED_RESPONSE,
                            final_url=current_url,
                            status_code=status,
                            content_type=content_type,
                        )
                    if redirects >= self._max_redirects:
                        self._log_failure(
                            FetchFailureCategory.TOO_MANY_REDIRECTS,
                            current_url,
                            status,
                        )
                        return self._failure(
                            requested_url,
                            FetchFailureCategory.TOO_MANY_REDIRECTS,
                            final_url=current_url,
                            status_code=status,
                            content_type=content_type,
                        )
                    current_url = urljoin(target.logical_url, location)
                    redirects += 1
                    last_final_url = target.logical_url
                    continue

                last_final_url = target.logical_url

                if not 200 <= status < 300:
                    self._log_failure(
                        FetchFailureCategory.HTTP_ERROR, current_url, status
                    )
                    return self._failure(
                        requested_url,
                        FetchFailureCategory.HTTP_ERROR,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )

                media_type = _media_type(raw_content_type)
                if media_type not in _HTML_MEDIA_TYPES:
                    self._log_failure(
                        FetchFailureCategory.UNSUPPORTED_CONTENT_TYPE,
                        current_url,
                        status,
                    )
                    return self._failure(
                        requested_url,
                        FetchFailureCategory.UNSUPPORTED_CONTENT_TYPE,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )

                declared_length = _content_length(response)
                if (
                    declared_length is not None
                    and declared_length > self._max_bytes
                ):
                    self._log_failure(
                        FetchFailureCategory.RESPONSE_TOO_LARGE,
                        current_url,
                        status,
                    )
                    return self._failure(
                        requested_url,
                        FetchFailureCategory.RESPONSE_TOO_LARGE,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )

                body = self._read_body(response, deadline)
                if isinstance(body, FetchFailureCategory):
                    self._log_failure(body, current_url, status)
                    return self._failure(
                        requested_url,
                        body,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )

                html = self._decode(body, response)
                try:
                    page_title, text, truncated = _extract_content(
                        html, self._max_text_chars
                    )
                except Exception:  # noqa: BLE001 - malformed provider markup
                    logger.warning(
                        "Website HTML extraction failed for host %s",
                        target.host,
                        exc_info=True,
                    )
                    return self._failure(
                        requested_url,
                        FetchFailureCategory.MALFORMED_RESPONSE,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )

                outcome = (
                    FetchOutcome.PARTIAL
                    if truncated or not text
                    else FetchOutcome.SUCCESS
                )
                return WebsiteFetchResult(
                    requested_url=requested_url,
                    final_url=current_url,
                    status_code=status,
                    content_type=content_type,
                    page_title=page_title,
                    text=text,
                    fetched_at=self._now(),
                    outcome=outcome,
                    truncated=truncated,
                )
            finally:
                response.close()

    # -- request/response helpers --------------------------------------
    def _build_request(
        self,
        target: ValidatedTarget,
        address: str,
        deadline: float,
    ) -> httpx.Request:
        headers = {
            "Host": target.host_header,
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9",
            "Accept-Encoding": "identity",
        }
        extensions = (
            {"sni_hostname": target.host}
            if target.scheme == "https"
            else {}
        )
        return self._client.build_request(
            "GET",
            target.connect_url(address),
            headers=headers,
            extensions=extensions,
            timeout=self._request_timeout(deadline),
        )

    def _request_timeout(self, deadline: float) -> httpx.Timeout:
        remaining = max(0.001, deadline - time.monotonic())
        connect = min(self._connect_timeout, remaining)
        read = min(self._read_timeout, remaining)
        return httpx.Timeout(
            connect=connect,
            read=read,
            write=read,
            pool=connect,
        )

    def _read_body(
        self, response: httpx.Response, deadline: float
    ) -> bytes | FetchFailureCategory:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self._max_bytes:
                    return FetchFailureCategory.RESPONSE_TOO_LARGE
                if time.monotonic() > deadline:
                    return FetchFailureCategory.TIMEOUT
                chunks.append(chunk)
        except httpx.TimeoutException:
            return FetchFailureCategory.TIMEOUT
        except httpx.HTTPError:
            return FetchFailureCategory.CONNECTION_ERROR
        except Exception:  # noqa: BLE001 - decoding or transport failure
            return FetchFailureCategory.MALFORMED_RESPONSE
        return b"".join(chunks)

    @staticmethod
    def _decode(body: bytes, response: httpx.Response) -> str:
        charset = response.charset_encoding or "utf-8"
        try:
            html = body.decode(charset, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
        return html.lstrip("\ufeff")

    def _failure(
        self,
        requested_url: str,
        category: FetchFailureCategory,
        *,
        final_url: str | None,
        status_code: int | None = None,
        content_type: str | None = None,
    ) -> WebsiteFetchResult:
        return WebsiteFetchResult(
            requested_url=requested_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            page_title=None,
            text="",
            fetched_at=self._now(),
            outcome=FetchOutcome.FAILED,
            failure_category=category,
        )

    def _now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(
            timezone.utc
        )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _log_failure(
        category: FetchFailureCategory,
        url: str | None,
        status: int | None,
    ) -> None:
        host = "-"
        if url:
            try:
                host = httpx.URL(url).host or "-"
            except Exception:  # noqa: BLE001 - logging must never fail
                host = "-"
        logger.info(
            "Website fetch failed: category=%s host=%s status=%s",
            category.value,
            host,
            status if status is not None else "-",
        )


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _extract_content(html: str, max_chars: int) -> tuple[str | None, str, bool]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    page_title: str | None = None
    if title_tag is not None:
        normalized_title = _normalize_inline(title_tag.get_text(" ", strip=True))
        if normalized_title:
            page_title = normalized_title[:MAX_TITLE_CHARS]

    for tag in soup.find_all(_REMOVABLE_TAGS):
        tag.decompose()
    for role in _REMOVABLE_ROLES:
        for tag in soup.find_all(attrs={"role": role}):
            tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()

    root = (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )
    text = _normalize_block(root.get_text(separator="\n"))
    truncated = len(text) > max_chars
    return page_title, text[:max_chars], truncated


def _normalize_inline(value: str) -> str:
    return " ".join(value.split())


def _normalize_block(value: str) -> str:
    lines = (_normalize_inline(line) for line in value.splitlines())
    return "\n".join(line for line in lines if line)
