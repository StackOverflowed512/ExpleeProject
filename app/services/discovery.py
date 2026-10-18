"""Explorer discovery: search, normalize, deduplicate, and persist.

The service depends on the :class:`SearchProvider` protocol only. It owns the
transaction boundary: either every candidate from one search is persisted or
the transaction is rolled back and a typed error is raised.

Update policy: when a later search returns an existing candidate, only
previously missing fields are filled. Known values (first-seen name, URL,
source, description) are never replaced, so provenance is preserved and a
sparse result cannot erase richer data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.schemas.discovery import (
    CandidateSummary,
    DiscoveryCounts,
    DiscoveryResponse,
)
from app.schemas.search import SearchResultItem
from app.services.discovery_urls import normalize_domain, normalize_website_url
from app.services.providers.search import SearchProvider

logger = logging.getLogger(__name__)


class DiscoveryPersistenceError(Exception):
    """Raised when candidates cannot be persisted."""


@dataclass(frozen=True)
class PreparedCandidate:
    """A provider result normalized for persistence."""

    name: str
    website_url: str
    domain: str
    description: str | None
    provider: str
    provider_result_id: str | None
    source_url: str


def prepare(item: SearchResultItem) -> PreparedCandidate | None:
    """Normalize one provider result; return ``None`` if it is unusable."""
    website_url = normalize_website_url(str(item.url))
    domain = normalize_domain(str(item.url))
    title = item.title.strip()
    if website_url is None or domain is None or not title:
        return None

    snippet = item.snippet.strip() if item.snippet else None
    return PreparedCandidate(
        name=title[:300],
        website_url=website_url,
        domain=domain,
        description=snippet[:2000] if snippet else None,
        provider=item.provider,
        provider_result_id=item.provider_result_id,
        source_url=website_url,
    )


def deduplicate(
    candidates: list[PreparedCandidate],
) -> list[PreparedCandidate]:
    """Collapse duplicates inside one batch, keeping the first occurrence.

    Identity is the normalized domain, falling back to
    ``(provider, provider_result_id)`` when the provider supplies an
    identifier. Names never participate, so companies with similar names are
    kept separate.
    """
    seen_domains: set[str] = set()
    seen_provider_ids: set[tuple[str, str]] = set()
    unique: list[PreparedCandidate] = []

    for candidate in candidates:
        provider_key = (
            (candidate.provider, candidate.provider_result_id)
            if candidate.provider_result_id is not None
            else None
        )
        if candidate.domain in seen_domains:
            continue
        if provider_key is not None and provider_key in seen_provider_ids:
            continue

        seen_domains.add(candidate.domain)
        if provider_key is not None:
            seen_provider_ids.add(provider_key)
        unique.append(candidate)

    return unique


def _find_existing(
    session: Session, candidate: PreparedCandidate
) -> CompanyCandidate | None:
    stmt = select(CompanyCandidate).where(
        CompanyCandidate.domain == candidate.domain
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return existing

    if candidate.provider_result_id is not None:
        stmt = select(CompanyCandidate).where(
            CompanyCandidate.provider == candidate.provider,
            CompanyCandidate.provider_result_id == candidate.provider_result_id,
        )
        return session.execute(stmt).scalar_one_or_none()
    return None


def _fill_missing_fields(
    existing: CompanyCandidate, candidate: PreparedCandidate
) -> bool:
    """Copy only previously missing values; return whether anything changed."""
    changed = False
    if existing.website_url is None:
        existing.website_url = candidate.website_url
        changed = True
    if existing.description is None and candidate.description:
        existing.description = candidate.description
        changed = True
    return changed


def _insert_returning_id(
    session: Session,
    values: dict[str, object],
    *,
    index_elements: list[object],
    index_where: object,
) -> int | None:
    stmt = (
        pg_insert(CompanyCandidate)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=index_elements,
            index_where=index_where,
        )
        .returning(CompanyCandidate.id)
    )
    return session.execute(stmt).scalar_one_or_none()


def _insert_or_get(
    session: Session, candidate: PreparedCandidate
) -> tuple[CompanyCandidate, bool]:
    """Insert a candidate, returning the row and whether it was created.

    Lookup happens first so provider identifiers deduplicate even when a
    later search resolves the same provider result to a different domain.
    The insert then runs inside a savepoint; if a concurrent transaction
    inserts the same row first, the savepoint is rolled back, the session
    stays usable, and the winner's row is read in the outer transaction.
    """
    existing = _find_existing(session, candidate)
    if existing is not None:
        return existing, False

    values: dict[str, object] = {
        "name": candidate.name,
        "website_url": candidate.website_url,
        "domain": candidate.domain,
        "description": candidate.description,
        "provider": candidate.provider,
        "provider_result_id": candidate.provider_result_id,
        "source_url": candidate.source_url,
    }

    try:
        with session.begin_nested():
            inserted_id = _insert_returning_id(
                session,
                values,
                index_elements=["domain"],
                index_where=CompanyCandidate.domain.isnot(None),
            )
            if inserted_id is None and candidate.provider_result_id is not None:
                inserted_id = _insert_returning_id(
                    session,
                    values,
                    index_elements=["provider", "provider_result_id"],
                    index_where=CompanyCandidate.provider_result_id.isnot(None),
                )
    except IntegrityError:
        # A concurrent insert won the race; the savepoint is already rolled
        # back, so resolve the winner in the outer transaction.
        winner = _find_existing(session, candidate)
        if winner is None:
            raise
        return winner, False

    if inserted_id is not None:
        row = session.get(CompanyCandidate, inserted_id)
        if row is None:  # pragma: no cover - defensive
            raise DiscoveryPersistenceError(
                "An inserted candidate could not be read back."
            )
        return row, True

    existing = _find_existing(session, candidate)
    if existing is None:
        raise DiscoveryPersistenceError(
            "A candidate conflicted but could not be located afterwards."
        )
    return existing, False


def run_discovery(
    session: Session,
    provider: SearchProvider,
    query: str,
    limit: int,
) -> DiscoveryResponse:
    """Run one discovery search and persist its candidates.

    Raises :class:`DiscoveryPersistenceError` after rolling back if the
    database rejects the transaction. Provider errors propagate unchanged.
    A blank query raises :class:`ValueError` before the provider is called.
    """
    if not query.strip():
        raise ValueError("Search query must not be empty.")

    items = provider.search(query, limit)

    prepared = [c for c in (prepare(item) for item in items) if c is not None]
    skipped = len(items) - len(prepared)
    unique = deduplicate(prepared)

    created = 0
    updated = 0
    persisted_ids: list[int] = []
    try:
        for candidate in unique:
            row, was_created = _insert_or_get(session, candidate)
            if not was_created and _fill_missing_fields(row, candidate):
                updated += 1
            if was_created:
                created += 1
            persisted_ids.append(row.id)
        session.commit()
    except (SQLAlchemyError, DiscoveryPersistenceError) as exc:
        session.rollback()
        logger.exception("Discovery persistence failed")
        raise DiscoveryPersistenceError(
            "Discovery results could not be saved."
        ) from exc

    rows = [session.get(CompanyCandidate, row_id) for row_id in persisted_ids]
    results = [
        CandidateSummary(
            id=row.id,
            name=row.name,
            website_url=row.website_url,
            domain=row.domain,
            description=row.description,
            provider=row.provider,
            provider_result_id=row.provider_result_id,
            source_url=row.source_url,
            discovered_at=row.discovered_at,
            updated_at=row.updated_at,
        )
        for row in rows
        if row is not None
    ]

    return DiscoveryResponse(
        query=query,
        provider=provider.name,
        requested_limit=limit,
        results=results,
        counts=DiscoveryCounts(
            returned=len(results),
            created=created,
            updated=updated,
            skipped=skipped,
        ),
    )
