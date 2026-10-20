"""Outbound URL validation and SSRF protection.

Every website request must pass through :func:`validate_public_url` before a
connection is opened, and again for every redirect target. The validator:

* accepts only absolute ``http``/``https`` URLs on the default ports;
* rejects embedded credentials, control characters, fragments, and hosts that
  could be interpreted as alternate numeric IP forms;
* resolves hostnames itself and requires *every* resolved address to be a
  globally routable unicast address (using :mod:`ipaddress`, not string
  matching);
* rejects known cloud metadata hostnames and addresses.

The HTTP adapter connects to a resolved IP address while preserving the
logical ``Host`` header (and TLS SNI). This closes the DNS-rebinding gap
between validation and connection for the adapter. DNS resolution itself is
performed by the operating system and is not independently timed; see
``docs/architecture.md`` for the documented limitation.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.services.providers.website import FetchFailureCategory

AddressResolver = Callable[[str, int], Sequence[str]]

MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_PORTS = frozenset({80, 443})
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NUMERIC_ONLY_HOST_RE = re.compile(r"^[0-9.]+$")

# Hostnames that resolve to local or provider-internal endpoints. ``.internal``
# covers instance metadata names used by major cloud providers.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
)

# Addresses that are not covered by ``is_global`` on every Python version but
# are well-known metadata or non-public service endpoints.
_BLOCKED_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure instance metadata (IPv4)
        "100.100.100.200",  # Alibaba Cloud metadata
        "168.63.129.16",  # Azure platform virtual IP
        "fd00:ec2::254",  # AWS instance metadata (IPv6)
    }
)


class UnsafeUrlError(Exception):
    """Raised when a URL cannot be fetched safely."""

    def __init__(
        self,
        category: FetchFailureCategory,
        message: str,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """A logical URL plus the public addresses it may connect to."""

    logical_url: str
    scheme: str
    host: str
    port: int
    path: str
    query: str
    addresses: tuple[str, ...]

    @property
    def host_header(self) -> str:
        """Value for the HTTP ``Host`` header, without a default port."""
        if ":" in self.host:
            return f"[{self.host}]"
        return self.host

    def connect_url(self, address: str) -> str:
        """Return the URL used to connect to ``address`` directly."""
        host = f"[{address}]" if ":" in address else address
        if self.port != _DEFAULT_PORTS[self.scheme]:
            host = f"{host}:{self.port}"
        return urlunsplit((self.scheme, host, self.path, self.query, ""))


def default_resolver(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` to every address returned by the system resolver."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [str(info[4][0]) for info in infos]
    return tuple(dict.fromkeys(addresses))


def validate_public_url(
    url: str,
    *,
    resolver: AddressResolver | None = None,
) -> ValidatedTarget:
    """Validate ``url`` and return its public connection targets.

    Raises :class:`UnsafeUrlError` with a stable failure category. The
    function performs DNS resolution when ``url`` contains a hostname; tests
    inject a deterministic ``resolver`` so no network access is required.
    """
    raw = (url or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL, "The website URL is invalid."
        )
    if any(character.isspace() for character in raw):
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL, "The website URL is invalid."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL, "The website URL is invalid."
        )
    if "\\" in raw:
        # Browsers may treat backslashes as path separators while URL parsers
        # do not; reject the ambiguity outright.
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL, "The website URL is invalid."
        )

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL, "The website URL is invalid."
        ) from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "Only http and https website URLs are supported.",
        )
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "Website URLs must not embed credentials.",
        )

    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid port.",
        ) from exc
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if port not in _ALLOWED_PORTS:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "Website URLs must use the default HTTP or HTTPS port.",
        )
    if port != _DEFAULT_PORTS[scheme]:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL uses a port that does not match its scheme.",
        )

    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL has no host.",
        )
    if "%" in host:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid host.",
        )

    path = parts.path or "/"
    query = parts.query
    normalized_host, addresses = _resolve_target(host, port, resolver)
    logical_url = urlunsplit(
        (
            scheme,
            _format_host(normalized_host, scheme, port),
            path,
            query,
            "",
        )
    )

    return ValidatedTarget(
        logical_url=logical_url,
        scheme=scheme,
        host=normalized_host,
        port=port,
        path=path,
        query=query,
        addresses=addresses,
    )


def _format_host(host: str, scheme: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]" if port == _DEFAULT_PORTS[scheme] else (
            f"[{host}]:{port}"
        )
    return host if port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"


def _resolve_target(
    host: str,
    port: int,
    resolver: AddressResolver | None,
) -> tuple[str, tuple[str, ...]]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        normalized_host = _normalize_hostname(host)
        resolve = resolver or default_resolver
        try:
            resolved = resolve(normalized_host, port)
        except (socket.gaierror, OSError) as exc:
            raise UnsafeUrlError(
                FetchFailureCategory.DNS_FAILURE,
                "The website host could not be resolved.",
            ) from exc
        if not resolved:
            raise UnsafeUrlError(
                FetchFailureCategory.DNS_FAILURE,
                "The website host could not be resolved.",
            )
    else:
        public = _ensure_public_address(literal)
        canonical = str(public)
        return canonical, (canonical,)

    ordered: list[str] = []
    for address in resolved:
        try:
            parsed = ipaddress.ip_address(str(address).strip())
        except ValueError as exc:
            raise UnsafeUrlError(
                FetchFailureCategory.DNS_FAILURE,
                "The website host resolved to an invalid address.",
            ) from exc
        public = _ensure_public_address(parsed)
        canonical = str(public)
        if canonical not in ordered:
            ordered.append(canonical)

    if not ordered:  # pragma: no cover - guarded by the empty check above
        raise UnsafeUrlError(
            FetchFailureCategory.DNS_FAILURE,
            "The website host could not be resolved.",
        )

    # Prefer IPv4 for compatibility; every entry was already validated.
    ordered.sort(key=lambda address: (":" in address, address))
    return normalized_host, tuple(ordered)


def _normalize_hostname(host: str) -> str:
    lowered = host.strip().rstrip(".").lower()
    if not lowered:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL has no host.",
        )

    try:
        ascii_host = lowered.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid host.",
        ) from exc
    ascii_host = ascii_host.rstrip(".")

    if (
        ascii_host in _BLOCKED_HOSTNAMES
        or ascii_host.endswith(_BLOCKED_HOST_SUFFIXES)
    ):
        raise UnsafeUrlError(
            FetchFailureCategory.BLOCKED_DESTINATION,
            "The website host is not a public destination.",
        )

    if len(ascii_host) > 253:
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid host.",
        )

    # Reject non-canonical numeric forms (for example ``127.1`` or
    # ``2130706433``) before the system resolver can reinterpret them as IPs.
    if _NUMERIC_ONLY_HOST_RE.fullmatch(ascii_host):
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an ambiguous numeric host.",
        )

    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid host.",
        )
    if not any(character.isalpha() for character in labels[-1]):
        raise UnsafeUrlError(
            FetchFailureCategory.INVALID_URL,
            "The website URL contains an invalid host.",
        )
    return ascii_host


def _ensure_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    if str(address) in _BLOCKED_ADDRESSES:
        raise UnsafeUrlError(
            FetchFailureCategory.BLOCKED_DESTINATION,
            "The website host is not a public destination.",
        )
    if (
        not address.is_global
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    ):
        raise UnsafeUrlError(
            FetchFailureCategory.BLOCKED_DESTINATION,
            "The website host is not a public destination.",
        )
    return address
