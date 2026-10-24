"""Deterministic fake website fetcher.

Used by tests and credential-free runs (``WEBSITE_FETCHER=fake``). It never
touches the network. Results are looked up by exact URL; unknown URLs return
a ``connection_failed`` result so nothing is ever fabricated. Every call is
recorded in :attr:`FakeWebsiteFetcher.calls`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult

FAKE_FETCHED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class FakeWebsiteFetcher:
    """In-memory fetcher returning preset results keyed by URL."""

    name: str = "fake"
    results: dict[str, FetchResult] = field(default_factory=dict)
    fetched_at: datetime = FAKE_FETCHED_AT
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        preset = self.results.get(url)
        if preset is not None:
            return replace(preset, requested_url=url, fetcher=self.name)
        return FetchResult(
            requested_url=url,
            fetched_at=self.fetched_at,
            outcome=FetchOutcome.FAILED,
            fetcher=self.name,
            failure=FetchFailure.CONNECTION_FAILED,
        )
