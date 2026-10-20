"""Unit tests for outbound URL validation and SSRF protection."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.services.providers.url_safety import (
    UnsafeUrlError,
    validate_public_url,
)
from app.services.providers.website import FetchFailureCategory

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "93.184.216.35"
PUBLIC_V6 = "2606:4700:4700::1111"


def _resolver(*addresses: str):
    calls: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> Sequence[str]:
        calls.append((host, port))
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _category(url: str, resolver=None) -> FetchFailureCategory:
    with pytest.raises(UnsafeUrlError) as excinfo:
        validate_public_url(url, resolver=resolver)
    return excinfo.value.category


def test_public_hostname_is_resolved_and_normalized() -> None:
    resolver = _resolver(PUBLIC_V6, PUBLIC_V4)

    target = validate_public_url(
        "HTTPS://Example.COM:443/About?x=1#fragment",
        resolver=resolver,
    )

    assert target.logical_url == "https://example.com/About?x=1"
    assert target.scheme == "https"
    assert target.host == "example.com"
    assert target.port == 443
    assert target.host_header == "example.com"
    # IPv4 is preferred for compatibility, but both were validated.
    assert target.addresses == (PUBLIC_V4, PUBLIC_V6)
    assert resolver.calls == [("example.com", 443)]  # type: ignore[attr-defined]


def test_connect_url_uses_resolved_ip_and_preserves_path() -> None:
    target = validate_public_url(
        "http://example.com/path?q=1",
        resolver=_resolver(PUBLIC_V4),
    )

    assert target.connect_url(PUBLIC_V4) == f"http://{PUBLIC_V4}/path?q=1"
    assert target.logical_url == "http://example.com/path?q=1"


def test_public_ip_literal_needs_no_resolution() -> None:
    resolver = _resolver("127.0.0.1")

    target = validate_public_url("https://8.8.8.8/", resolver=resolver)

    assert target.addresses == ("8.8.8.8",)
    assert resolver.calls == []  # type: ignore[attr-defined]


def test_public_ipv6_literal_is_formatted_with_brackets() -> None:
    target = validate_public_url(f"https://[{PUBLIC_V6}]/", resolver=_resolver())

    assert target.host_header == f"[{PUBLIC_V6}]"
    assert target.logical_url == f"https://[{PUBLIC_V6}]/"


def test_ipv4_mapped_ipv6_is_canonicalized() -> None:
    target = validate_public_url(
        "https://[::ffff:8.8.8.8]/", resolver=_resolver()
    )

    assert target.addresses == ("8.8.8.8",)


def test_idn_hostname_is_resolved_in_ascii() -> None:
    resolver = _resolver(PUBLIC_V4)

    target = validate_public_url("https://bücher.example/", resolver=resolver)

    assert target.host == "xn--bcher-kva.example"
    assert resolver.calls == [("xn--bcher-kva.example", 443)]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "mailto:someone@example.com",
        "http://",
        "https:///path",
        "http://user:secret@example.com/",
        "http://user@example.com/",
        "http://@example.com/",
        "http://example.com:8080/",
        "https://example.com:80/",
        "http://example.com:443/",
        "http://example.com:99999/",
        "http://example.com:notaport/",
        "http://exa mple.com/",
        "http://exa\nmple.com/",
        "http://example.com\\@127.0.0.1/",
        "http://example.com/" + "a" * 2100,
    ],
)
def test_invalid_urls_are_rejected(url: str) -> None:
    assert _category(url, _resolver(PUBLIC_V4)) is FetchFailureCategory.INVALID_URL


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "service.local",
        "printer.localdomain",
        "app.internal",
        "router.home.arpa",
    ],
)
def test_local_and_metadata_hostnames_are_blocked(host: str) -> None:
    resolver = _resolver(PUBLIC_V4)

    assert (
        _category(f"http://{host}/", resolver)
        is FetchFailureCategory.BLOCKED_DESTINATION
    )
    assert resolver.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "[::1]",
        "[fc00::1]",
        "[fe80::1]",
        "[::ffff:127.0.0.1]",
    ],
)
def test_non_public_ip_destinations_are_blocked(host: str) -> None:
    category = _category(f"http://{host}/", _resolver(PUBLIC_V4))

    assert category in {
        FetchFailureCategory.BLOCKED_DESTINATION,
        FetchFailureCategory.INVALID_URL,
    }


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.20.0.1",
        "192.168.0.5",
        "169.254.169.254",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:169.254.169.254",
    ],
)
def test_hostname_resolving_to_non_public_address_is_blocked(
    address: str,
) -> None:
    assert (
        _category("https://example.com/", _resolver(address))
        is FetchFailureCategory.BLOCKED_DESTINATION
    )


def test_any_private_address_in_a_resolution_is_rejected() -> None:
    resolver = _resolver(PUBLIC_V4, "10.0.0.1")

    assert (
        _category("https://example.com/", resolver)
        is FetchFailureCategory.BLOCKED_DESTINATION
    )


def test_unresolvable_host_is_a_dns_failure() -> None:
    def resolver(host: str, port: int) -> Sequence[str]:
        import socket

        raise socket.gaierror("no such host")

    assert (
        _category("https://missing.example/", resolver)
        is FetchFailureCategory.DNS_FAILURE
    )


def test_empty_resolution_is_a_dns_failure() -> None:
    assert (
        _category("https://missing.example/", _resolver())
        is FetchFailureCategory.DNS_FAILURE
    )


def test_invalid_resolved_address_is_a_dns_failure() -> None:
    assert (
        _category("https://missing.example/", _resolver("not-an-ip"))
        is FetchFailureCategory.DNS_FAILURE
    )
