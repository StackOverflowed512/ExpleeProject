"""Integration tests for discovery persistence against PostgreSQL.

These tests require ``TEST_DATABASE_URL`` pointing at a disposable database
(for example ``explee_test``); they are skipped otherwise and always truncate
``company_candidates`` before each test.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.schemas.search import SearchResultItem
from app.services.discovery import run_discovery
from app.services.providers.fake import FakeSearchProvider


def _item(
    url: str,
    *,
    title: str = "Acme Corp",
    snippet: str | None = "Acme builds widgets.",
    provider: str = "fake",
    pid: str | None = None,
) -> SearchResultItem:
    return SearchResultItem(
        title=title,
        url=url,
        snippet=snippet,
        provider=provider,
        provider_result_id=pid,
    )


def _run(
    session: Session,
    items: list[SearchResultItem],
    *,
    query: str = "acme",
    limit: int = 5,
):
    provider = FakeSearchProvider(results=items)
    return run_discovery(session, provider, query, limit)


def test_successful_discovery_persists_candidates(db_session: Session) -> None:
    response = _run(
        db_session,
        [
            _item("https://acme.com", title="Acme Corp"),
            _item("https://beta.io", title="Beta"),
        ],
    )

    assert response.counts.created == 2
    assert response.counts.skipped == 0
    rows = db_session.execute(select(CompanyCandidate)).scalars().all()
    assert {row.domain for row in rows} == {"acme.com", "beta.io"}
    assert all(row.source_url for row in rows)
    assert all(row.discovered_at is not None for row in rows)


def test_duplicates_within_one_search_are_collapsed(db_session: Session) -> None:
    response = _run(
        db_session,
        [
            _item("https://acme.com", title="Acme"),
            _item("https://www.acme.com/about", title="Acme About"),
            _item("https://acme.com", title="Acme duplicate"),
        ],
    )

    assert response.counts.created == 1
    assert response.counts.returned == 1


def test_duplicate_across_searches_updates_missing_fields(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    first = _run(
        db_session,
        [_item("https://acme.com", title="Acme", snippet=None)],
    )
    assert first.counts.created == 1

    with db_session_factory() as second_session:
        second = _run(
            second_session,
            [
                _item(
                    "https://acme.com",
                    title="Acme renamed",
                    snippet="Now with a description.",
                )
            ],
        )

    assert second.counts.created == 0
    assert second.counts.updated == 1

    db_session.expire_all()
    row = db_session.execute(
        select(CompanyCandidate).where(CompanyCandidate.domain == "acme.com")
    ).scalar_one()
    # First-seen name and provenance are preserved; only the gap was filled.
    assert row.name == "Acme"
    assert row.description == "Now with a description."
    assert row.source_url == "https://acme.com/"


def test_known_fields_are_not_overwritten_by_later_values(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    _run(db_session, [_item("https://acme.com", title="Acme", snippet="First")])

    with db_session_factory() as second_session:
        _run(second_session, [_item("https://acme.com", title="Other", snippet="Second")])

    db_session.expire_all()
    row = db_session.execute(
        select(CompanyCandidate).where(CompanyCandidate.domain == "acme.com")
    ).scalar_one()
    assert row.name == "Acme"
    assert row.description == "First"


def test_candidate_with_missing_optional_fields_is_persisted(
    db_session: Session,
) -> None:
    response = _run(db_session, [_item("https://bare.dev", snippet=None)])

    assert response.counts.created == 1
    assert response.results[0].description is None


def test_invalid_urls_are_skipped_and_counted(db_session: Session) -> None:
    response = _run(
        db_session,
        [
            _item("https://good.com"),
            # model_construct: these would be rejected by the strict schema,
            # but the service must still defend against unusable URLs.
            SearchResultItem.model_construct(
                title="Broken",
                url="https://bad host",
                snippet=None,
                provider="fake",
                provider_result_id=None,
            ),
            SearchResultItem.model_construct(
                title="FTP",
                url="ftp://files.example.com",
                snippet=None,
                provider="fake",
                provider_result_id=None,
            ),
        ],
    )

    assert response.counts.skipped == 2
    assert response.counts.created == 1


def test_provider_identifier_deduplicates_across_domains(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    _run(db_session, [_item("https://acme.com", pid="r-1")])

    with db_session_factory() as second_session:
        response = _run(second_session, [_item("https://acme-cdn.com", pid="r-1")])

    assert response.counts.created == 0
    assert db_session.execute(select(CompanyCandidate)).scalars().all().__len__() == 1


def test_similar_names_with_different_domains_are_not_merged(
    db_session: Session,
) -> None:
    response = _run(
        db_session,
        [
            _item("https://acme.com", title="Acme"),
            _item("https://acme-labs.com", title="Acme"),
        ],
    )

    assert response.counts.created == 2


def test_empty_provider_results_yield_empty_response(db_session: Session) -> None:
    response = _run(db_session, [])

    assert response.results == []
    assert response.counts.created == 0
    assert response.counts.returned == 0
