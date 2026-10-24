"""URL and destination validation for outbound website fetches (SSRF guard).

Research fetches URLs that came from third-party search results, so every
URL is treated as untrusted input. Validation happens in two stages:

1. :func:`check_url` — syntax only, no network. Accepts absolute ``http`` or
   ``https`` URLs on ports 80/443 without userinfo, whose host is either a
   canonical public IP literal or a well-formed DNS name. Alternate numeric
   host forms (``2130706433``, ``0x7f.1``, ``127.1``, ``0177.0.0.1``) are
   rejected outright instead of being left to the OS resolver.
2. :func:`resolve_public_addresses` — resolves the host and requires *every*
   returned address to be public. A name that resolves to any loopback,
   private, link-local, multicast, reserved, shared (CGNAT), or otherwise
   non-global address is rejected, which also covers cloud metadata
   endpoints such as ``169.254.169.254`` and ``fd00:ec2::254``.

The fetcher then connects to one of the validated addresses directly (DNS
pinning), so a second, attacker-controlled DNS answer cannot redirect the
connection after validation. See ``docs/architecture.md`` for the residual
limitations.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.services.providers.website import FetchFailure

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
# Company websites are served on the standard ports. Refusing everything else
# keeps the fetcher from being pointed at arbitrary services on public hosts.
ALLOWED_PORTS = frozenset({80, 443})
MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# A top-level label must start with a letter. This rejects every purely
# numeric or hex-looking host (``127.1``, ``0x7f.0x1``) that a resolver
# would otherwise interpret as an IPv4 address.
_TOP_LEVEL_LABEL = re.compile(r"^[a-z][a-z0-9-]*$")
_BLOCKED_NAMES = frozenset({"localhost"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


class UnsafeUrlError(Exception):
    """A URL or destination was rejected before any connection was made."""

    def __init__(self, failure: FetchFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class ValidatedUrl:
    """A syntactically safe URL split into the parts the fetcher needs."""

    url: str
    scheme: str
    host: str
    port: int
    is_ip_literal: bool
    path_and_query: str


def _parse_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return an IPv4 address tunnelled inside ``address``, if any."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_public_address(address: IPAddress) -> bool:
    """Return whether ``address`` is a globally routable unicast address."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None and not is_public_address(embedded):
            return False
    return not (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _normalize_hostname(raw_host: str) -> str:
    host = raw_host.strip().lower().rstrip(".")
    if not host:
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL has no host.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeUrlError(
            FetchFailure.INVALID_URL, "The URL host is not a valid hostname."
        ) from None
    if len(host) > MAX_HOSTNAME_LENGTH:
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL host is too long.")

    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError(
            FetchFailure.BLOCKED_DESTINATION,
            "The URL host is a local or internal name.",
        )
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise UnsafeUrlError(
            FetchFailure.INVALID_URL, "The URL host is not a valid hostname."
        )
    if not _TOP_LEVEL_LABEL.match(labels[-1]):
        raise UnsafeUrlError(
            FetchFailure.INVALID_URL,
            "The URL host uses an unsupported numeric form.",
        )
    return host


def check_url(url: str) -> ValidatedUrl:
    """Validate ``url`` without network access.

    Raises :class:`UnsafeUrlError` with a safe failure category when the URL
    must not be fetched.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL is empty.")
    candidate = url.strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL is too long.")
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in candidate):
        raise UnsafeUrlError(
            FetchFailure.INVALID_URL, "The URL contains invalid characters."
        )

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        raise UnsafeUrlError(
            FetchFailure.INVALID_URL, "The URL could not be parsed."
        ) from None

    scheme = parts.scheme.lower()
    if not scheme:
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL is not absolute.")
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            FetchFailure.DISALLOWED_SCHEME, "Only http and https URLs are fetched."
        )
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeUrlError(
            FetchFailure.CREDENTIALS_IN_URL,
            "URLs with embedded credentials are not fetched.",
        )
    if parts.hostname is None:
        raise UnsafeUrlError(FetchFailure.INVALID_URL, "The URL has no host.")

    effective_port = port if port is not None else DEFAULT_PORTS[scheme]
    if effective_port not in ALLOWED_PORTS:
        raise UnsafeUrlError(
            FetchFailure.DISALLOWED_PORT, "Only ports 80 and 443 are fetched."
        )

    literal = _parse_ip_literal(parts.hostname)
    if literal is not None:
        if isinstance(literal, ipaddress.IPv6Address) and "[" not in parts.netloc:
            raise UnsafeUrlError(
                FetchFailure.INVALID_URL, "The URL host is malformed."
            )
        if not is_public_address(literal):
            raise UnsafeUrlError(
                FetchFailure.BLOCKED_DESTINATION,
                "The URL points to a non-public address.",
            )
        host = literal.compressed
        is_ip_literal = True
    else:
        host = _normalize_hostname(parts.hostname)
        is_ip_literal = False

    path = parts.path or "/"
    path_and_query = f"{path}?{parts.query}" if parts.query else path
    netloc_host = f"[{host}]" if ":" in host else host
    netloc = (
        netloc_host
        if effective_port == DEFAULT_PORTS[scheme]
        else f"{netloc_host}:{effective_port}"
    )
    return ValidatedUrl(
        url=f"{scheme}://{netloc}{path_and_query}",
        scheme=scheme,
        host=host,
        port=effective_port,
        is_ip_literal=is_ip_literal,
        path_and_query=path_and_query,
    )


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` with the operating system resolver."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def resolve_public_addresses(
    target: ValidatedUrl,
    resolver: Resolver,
) -> list[IPAddress]:
    """Resolve ``target`` and return its addresses if all are public.

    Fails closed: if any resolved address is non-public the whole host is
    rejected, so a name that mixes public and private records cannot be
    used to reach an internal service.
    """
    if target.is_ip_literal:
        return [ipaddress.ip_address(target.host)]

    try:
        raw_addresses = resolver(target.host, target.port)
    except (OSError, UnicodeError):
        raise UnsafeUrlError(
            FetchFailure.DNS_RESOLUTION_FAILED, "The website host could not be resolved."
        ) from None

    addresses: list[IPAddress] = []
    for raw in raw_addresses:
        # Strip an IPv6 zone index (``fe80::1%eth0``); zoned addresses are
        # link-local and are rejected below anyway.
        parsed = _parse_ip_literal(raw.split("%", 1)[0])
        if parsed is None:
            raise UnsafeUrlError(
                FetchFailure.DNS_RESOLUTION_FAILED,
                "The website host resolved to an unreadable address.",
            )
        if not is_public_address(parsed):
            raise UnsafeUrlError(
                FetchFailure.BLOCKED_DESTINATION,
                "The website host resolves to a non-public address.",
            )
        if parsed not in addresses:
            addresses.append(parsed)

    if not addresses:
        raise UnsafeUrlError(
            FetchFailure.DNS_RESOLUTION_FAILED, "The website host could not be resolved."
        )
    return addresses
