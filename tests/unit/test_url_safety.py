"""Unit tests for SSRF URL and destination validation. No network access."""

from __future__ import annotations

import ipaddress

import pytest

from app.services.providers.url_safety import (
    UnsafeUrlError,
    check_url,
    is_public_address,
    resolve_public_addresses,
)
from app.services.providers.website import FetchFailure

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _rejects(url: str, failure: FetchFailure) -> None:
    with pytest.raises(UnsafeUrlError) as caught:
        check_url(url)
    assert caught.value.failure is failure


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Acme.COM", "https://acme.com/"),
        ("http://acme.com:80/about?x=1#frag", "http://acme.com/about?x=1"),
        ("https://acme.com:443/", "https://acme.com/"),
        ("https://www.acme.com./", "https://www.acme.com/"),
        ("https://bücher.de/", "https://xn--bcher-kva.de/"),
        (f"https://{PUBLIC_V4}/", f"https://{PUBLIC_V4}/"),
        (f"https://[{PUBLIC_V6}]/", f"https://[{PUBLIC_V6}]/"),
    ],
)
def test_valid_public_urls_are_normalized(url: str, expected: str) -> None:
    assert check_url(url).url == expected


@pytest.mark.parametrize(
    "url",
    ["", "   ", "acme.com", "//acme.com", "https://", "https://acme .com", "https://a\\b.com"],
)
def test_malformed_urls_are_rejected(url: str) -> None:
    _rejects(url, FetchFailure.INVALID_URL)


@pytest.mark.parametrize(
    "url",
    ["ftp://acme.com", "file:///etc/passwd", "gopher://acme.com", "javascript:alert(1)", "data:text/html,x"],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    _rejects(url, FetchFailure.DISALLOWED_SCHEME)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@acme.com/",
        "https://user@acme.com/",
        "https://@acme.com/",
        "https://acme.com@169.254.169.254/",
    ],
)
def test_embedded_credentials_are_rejected(url: str) -> None:
    _rejects(url, FetchFailure.CREDENTIALS_IN_URL)


@pytest.mark.parametrize(
    ("url", "failure"),
    [
        ("https://acme.com:8443/", FetchFailure.DISALLOWED_PORT),
        ("http://acme.com:22/", FetchFailure.DISALLOWED_PORT),
        ("https://acme.com:0/", FetchFailure.DISALLOWED_PORT),
        ("https://acme.com:99999/", FetchFailure.INVALID_URL),
        ("https://acme.com:abc/", FetchFailure.INVALID_URL),
    ],
)
def test_invalid_or_nonstandard_ports_are_rejected(
    url: str, failure: FetchFailure
) -> None:
    _rejects(url, failure)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.64.0.1/",  # shared address space (CGNAT)
        "http://0.0.0.0/",
        "http://224.0.0.1/",  # multicast
        "http://240.0.0.1/",  # reserved
        "http://255.255.255.255/",
        "http://192.0.2.10/",  # documentation range
        "http://[::1]/",
        "http://[::]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[fd00:ec2::254]/",  # AWS IPv6 metadata
        "http://[ff02::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:169.254.169.254]/",
        "http://[2002:7f00:1::]/",  # 6to4 wrapping 127.0.0.1
        "http://[64:ff9b::a00:1]/",  # NAT64 wrapping 10.0.0.1
    ],
)
def test_non_public_ip_literals_are_blocked(url: str) -> None:
    _rejects(url, FetchFailure.BLOCKED_DESTINATION)


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",
        "http://0x7f.0x0.0x0.0x1/",
        "http://0177.0.0.1/",  # octal
        "http://127.1/",
        "http://127.0.1/",
        "http://017700000001/",
        "http://1.2.3.4.5/",
    ],
)
def test_alternate_numeric_host_forms_are_rejected(url: str) -> None:
    _rejects(url, FetchFailure.INVALID_URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://LOCALHOST./",
        "http://app.localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://router.home.arpa/",
    ],
)
def test_local_and_internal_names_are_blocked(url: str) -> None:
    _rejects(url, FetchFailure.BLOCKED_DESTINATION)


@pytest.mark.parametrize(
    "url",
    [
        "http://intranet/",  # single label
        "http://-bad.com/",
        "http://bad-.com/",
        "http://a..com/",
        "http://under_score.com/",
        "http://acme.com%2f@evil/",
        f"http://{'a' * 64}.com/",
    ],
)
def test_malformed_hostnames_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as caught:
        check_url(url)
    assert caught.value.failure in {
        FetchFailure.INVALID_URL,
        FetchFailure.CREDENTIALS_IN_URL,
    }


def test_overlong_url_is_rejected() -> None:
    _rejects("https://acme.com/" + "a" * 3000, FetchFailure.INVALID_URL)


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "8.8.8.8", "1.1.1.1"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))


def test_resolution_requires_every_address_to_be_public() -> None:
    target = check_url("https://mixed.example.com/")

    def resolver(host: str, port: int) -> list[str]:
        assert (host, port) == ("mixed.example.com", 443)
        return [PUBLIC_V4, "10.1.2.3"]

    with pytest.raises(UnsafeUrlError) as caught:
        resolve_public_addresses(target, resolver)
    assert caught.value.failure is FetchFailure.BLOCKED_DESTINATION


def test_resolution_to_metadata_address_is_blocked() -> None:
    target = check_url("http://rebind.example.com/")
    with pytest.raises(UnsafeUrlError) as caught:
        resolve_public_addresses(target, lambda host, port: ["169.254.169.254"])
    assert caught.value.failure is FetchFailure.BLOCKED_DESTINATION


def test_resolution_to_zoned_link_local_is_blocked() -> None:
    target = check_url("http://zone.example.com/")
    with pytest.raises(UnsafeUrlError) as caught:
        resolve_public_addresses(target, lambda host, port: ["fe80::1%eth0"])
    assert caught.value.failure is FetchFailure.BLOCKED_DESTINATION


def test_resolution_failure_is_categorized() -> None:
    def resolver(host: str, port: int) -> list[str]:
        raise OSError("nodename nor servname provided")

    target = check_url("https://nowhere.example.com/")
    with pytest.raises(UnsafeUrlError) as caught:
        resolve_public_addresses(target, resolver)
    assert caught.value.failure is FetchFailure.DNS_RESOLUTION_FAILED


def test_empty_resolution_is_a_failure() -> None:
    target = check_url("https://empty.example.com/")
    with pytest.raises(UnsafeUrlError) as caught:
        resolve_public_addresses(target, lambda host, port: [])
    assert caught.value.failure is FetchFailure.DNS_RESOLUTION_FAILED


def test_public_resolution_returns_unique_addresses() -> None:
    target = check_url("https://acme.com/")
    addresses = resolve_public_addresses(
        target, lambda host, port: [PUBLIC_V4, PUBLIC_V4, PUBLIC_V6]
    )
    assert [str(a) for a in addresses] == [PUBLIC_V4, PUBLIC_V6]


def test_ip_literal_is_not_resolved() -> None:
    def resolver(host: str, port: int) -> list[str]:
        raise AssertionError("IP literals must not be resolved")

    target = check_url(f"https://{PUBLIC_V4}/")
    assert [str(a) for a in resolve_public_addresses(target, resolver)] == [PUBLIC_V4]
