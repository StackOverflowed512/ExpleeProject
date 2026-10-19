"""Search provider interface and normalized result contract.

Discovery business logic depends only on :class:`SearchProvider`; vendor
adapters live in sibling modules and are selected through settings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.search import SearchResultItem


@runtime_checkable
class SearchProvider(Protocol):
    """Contract every web search adapter must satisfy."""

    name: str

    def search(self, query: str, limit: int) -> list[SearchResultItem]:
        """Return up to ``limit`` normalized results for ``query``.

        Implementations must raise a :class:`SearchProviderError` subclass on
        failure and must not fabricate results.
        """
        ...
