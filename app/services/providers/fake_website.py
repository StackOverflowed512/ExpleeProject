"""Deterministic in-memory website fetcher for tests.

The fake performs no network or DNS access. Tests configure results per URL
so every failure mode can be exercised without live websites.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetchResult,
)

Clock = Callable[[], datetime]


@dataclass
class FakeWebsiteFetcher:
    """Return configured results or exceptions for requested URLs."""

    name: str = "fake"
    by_url: dict[str, WebsiteFetchResult | Exception] = field(
        default_factory=dict
    )
    default: WebsiteFetchResult | Exception | None = None
    calls: list[str] = field(default_factory=list)
    clock: Clock = lambda: datetime.now(timezone.utc)

    def fetch(self, url: str) -> WebsiteFetchResult:
        self.calls.append(url)
        configured = self.by_url.get(url, self.default)
        if isinstance(configured, Exception):
            raise configured
        if configured is not None:
            return configured

        # No invented content: an unconfigured URL is a deterministic failed
        # attempt, which is useful for tests that only assert failure paths.
        return WebsiteFetchResult(
            requested_url=url,
            final_url=url,
            status_code=None,
            content_type=None,
            page_title=None,
            text="",
            fetched_at=self.clock(),
            outcome=FetchOutcome.FAILED,
            failure_category=FetchFailureCategory.CONNECTION_ERROR,
        )
