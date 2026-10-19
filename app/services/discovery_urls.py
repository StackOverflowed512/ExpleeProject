"""URL normalization helpers for company candidates."""

from __future__ import annotations

from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def normalize_domain(url: str) -> str | None:
    """Return a deduplication domain for ``url``, or ``None`` if unusable.

    Rules: only http/https URLs with a host are accepted; userinfo, port,
    path, query and fragment are discarded; the host is lowercased, a
    trailing dot and a leading ``www.`` are removed, and IDN hosts are
    converted to their ASCII form. No network access is performed.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    if parts.username or parts.password:
        return None

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return None

    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        # Bare hostnames are not usable company websites (e.g. "localhost").
        return None

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    if len(host) > 253:
        return None
    return host


def normalize_website_url(url: str) -> str | None:
    """Return a canonical absolute website URL, or ``None`` if unusable."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    if not parts.hostname:
        return None

    host = parts.hostname.lower().rstrip(".")
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    return parts._replace(
        scheme=scheme,
        netloc=netloc,
        path=path,
        query=parts.query,
        fragment="",
    ).geturl()
