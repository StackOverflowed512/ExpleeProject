"""Unit tests for website research configuration and the fetcher factory."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.providers import (
    FakeWebsiteFetcher,
    HttpWebsiteFetcher,
    build_website_fetcher,
)
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetchResult,
)


def test_research_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.website_fetch_connect_timeout_seconds == 5.0
    assert settings.website_fetch_read_timeout_seconds == 10.0
    assert settings.website_fetch_total_timeout_seconds == 15.0
    assert settings.website_fetch_max_bytes == 2_000_000
    assert settings.website_fetch_max_redirects == 3
    assert settings.website_fetch_max_text_chars == 20_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("website_fetch_connect_timeout_seconds", 0),
        ("website_fetch_connect_timeout_seconds", 31),
        ("website_fetch_read_timeout_seconds", 0),
        ("website_fetch_read_timeout_seconds", 61),
        ("website_fetch_total_timeout_seconds", 0),
        ("website_fetch_total_timeout_seconds", 121),
        ("website_fetch_max_bytes", 1023),
        ("website_fetch_max_bytes", 10_000_001),
        ("website_fetch_max_redirects", -1),
        ("website_fetch_max_redirects", 11),
        ("website_fetch_max_text_chars", 99),
        ("website_fetch_max_text_chars", 200_001),
    ],
)
def test_research_bounds_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_factory_builds_http_fetcher_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        website_fetch_max_redirects=7,
        website_fetch_max_text_chars=1234,
    )

    fetcher = build_website_fetcher(settings)

    assert isinstance(fetcher, HttpWebsiteFetcher)
    fetcher.close()


def test_fake_fetcher_returns_configured_results_and_records_calls() -> None:
    result = WebsiteFetchResult(
        requested_url="https://acme.com/",
        final_url="https://acme.com/",
        fetched_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        outcome=FetchOutcome.SUCCESS,
        status_code=200,
        content_type="text/html",
        page_title="Acme",
        text="Widgets.",
    )
    fetcher = FakeWebsiteFetcher(by_url={"https://acme.com/": result})

    assert fetcher.fetch("https://acme.com/") is result
    assert fetcher.calls == ["https://acme.com/"]


def test_fake_fetcher_raises_configured_exceptions() -> None:
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": RuntimeError("boom")}
    )

    with pytest.raises(RuntimeError):
        fetcher.fetch("https://acme.com/")


def test_fake_fetcher_unconfigured_url_is_a_typed_failure() -> None:
    result = FakeWebsiteFetcher().fetch("https://acme.com/")

    assert result.outcome is FetchOutcome.FAILED
    assert result.failure_category is FetchFailureCategory.CONNECTION_ERROR
    assert result.text == ""
    assert result.page_title is None
