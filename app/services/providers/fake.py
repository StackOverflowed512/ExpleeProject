"""Deterministic fake search provider.

Used when ``SEARCH_PROVIDER=fake`` (the default) and by tests. It performs no
network calls and returns a configured list of results, so the application
and test suite work without search credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.search import SearchResultItem


@dataclass
class FakeSearchProvider:
    """In-memory provider returning preset results."""

    name: str = "fake"
    results: list[SearchResultItem] = field(default_factory=list)

    def search(self, query: str, limit: int) -> list[SearchResultItem]:
        return list(self.results[:limit])
