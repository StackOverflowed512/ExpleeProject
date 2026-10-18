"""Unit tests for :mod:`app.services.discovery_urls` and normalization."""

from __future__ import annotations

import pytest

from app.schemas.search import SearchResultItem
from app.services.discovery import PreparedCandidate, deduplicate, prepare
from app.services.discovery_urls import normalize_domain, normalize_website_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.com", "acme.com"),
        ("https://www.acme.com/path?x=1", "acme.com"),
        ("http://ACME.COM./", "acme.com"),
        ("https://sub.acme.example.io/about", "sub.acme.example.io"),
        ("https://www.acme.com:8443/", "acme.com"),
        ("https://münchen.de", "xn--mnchen-3ya.de"),
    ],
)
def test_normalize_domain(url: str, expected: str) -> None:
    assert normalize_domain(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://acme.com",
        "mailto:sales@acme.com",
        "https://",
        "https://localhost",
        "https://user:pass@acme.com",
        "not a url",
        "",
    ],
)
def test_normalize_domain_rejects_unusable_urls(url: str) -> None:
    assert normalize_domain(url) is None


def test_normalize_website_url_adds_path_and_drops_fragment() -> None:
    assert normalize_website_url("HTTPS://WWW.Acme.com/#team") == (
        "https://www.acme.com/"
    )
    assert normalize_website_url("https://acme.com/about?ref=x#top") == (
        "https://acme.com/about?ref=x"
    )


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x.y", "https://"])
def test_normalize_website_url_rejects_non_http(url: str) -> None:
    assert normalize_website_url(url) is None


def _item(url: str, *, title: str = "Acme", pid: str | None = None) -> SearchResultItem:
    # model_construct bypasses field validation so tests can feed the
    # normalization layer values a strict schema would reject.
    return SearchResultItem.model_construct(
        title=title,
        url=url,
        snippet="Snippet",
        provider="fake",
        provider_result_id=pid,
    )


def test_prepare_rejects_invalid_urls() -> None:
    assert prepare(_item("mailto:a@b.co")) is None


def test_prepare_trims_and_bounds_text() -> None:
    candidate = prepare(
        SearchResultItem.model_construct(
            title=f"  {'A' * 400}  ",
            url="https://acme.com",
            snippet="  " + "s" * 3000,
            provider="fake",
            provider_result_id=None,
        )
    )
    assert candidate is not None
    assert len(candidate.name) == 300
    assert candidate.description is not None
    assert len(candidate.description) == 2000


def _candidate(domain: str, pid: str | None = None) -> PreparedCandidate:
    return PreparedCandidate(
        name="Acme",
        website_url=f"https://{domain}",
        domain=domain,
        description=None,
        provider="fake",
        provider_result_id=pid,
        source_url=f"https://{domain}",
    )


def test_deduplicate_uses_domain() -> None:
    unique = deduplicate([_candidate("a.com"), _candidate("a.com"), _candidate("b.com")])
    assert [c.domain for c in unique] == ["a.com", "b.com"]


def test_deduplicate_uses_provider_id_when_domains_differ() -> None:
    # Same provider identity but different hosts still collapses to one row.
    unique = deduplicate(
        [_candidate("a.com", pid="1"), _candidate("b.com", pid="1")]
    )
    assert len(unique) == 1


def test_deduplicate_does_not_merge_similar_names() -> None:
    first = _candidate("acme.com")
    second = PreparedCandidate(
        name="Acme",
        website_url="https://acme-other.com",
        domain="acme-other.com",
        description=None,
        provider="fake",
        provider_result_id=None,
        source_url="https://acme-other.com",
    )
    assert len(deduplicate([first, second])) == 2
