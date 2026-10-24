"""Integration tests for the research service against PostgreSQL.

Requires ``TEST_DATABASE_URL`` (skipped otherwise). The website fetcher is
always :class:`FakeWebsiteFetcher`; no network access happens.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.models.company_research import CompanyResearch
from app.services import research as research_service
from app.services.enrichment import get_enrichment
from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult
from app.services.providers.website_fake import FakeWebsiteFetcher
from app.services.research import (
    HISTORY_LIMIT,
    CandidateNotFoundError,
    CandidateWebsiteMissingError,
    ResearchPersistenceError,
    run_research,
)

URL = "https://acme.com/"
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def _ok(at: datetime = T0, **overrides: object) -> FetchResult:
    values: dict[str, object] = {
        "requested_url": URL,
        "fetched_at": at,
        "outcome": FetchOutcome.SUCCESS,
        "fetcher": "fake",
        "final_url": "https://acme.com/en",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "page_title": "Acme Widgets",
        "text": "Widgets that ship",
    }
    values.update(overrides)
    return FetchResult(**values)  # type: ignore[arg-type]


def _failed(at: datetime = T0, failure: FetchFailure = FetchFailure.TIMEOUT) -> FetchResult:
    return FetchResult(
        requested_url=URL,
        fetched_at=at,
        outcome=FetchOutcome.FAILED,
        fetcher="fake",
        final_url=URL,
        failure=failure,
    )


def _candidate(
    session: Session,
    *,
    website_url: str | None = URL,
    domain: str | None = "acme.com",
) -> CompanyCandidate:
    candidate = CompanyCandidate(
        name="Acme Corp",
        website_url=website_url,
        domain=domain,
        description="Acme builds widgets.",
        provider="tavily",
        provider_result_id=f"r-{domain}" if domain else None,
        source_url="https://acme.com/",
    )
    session.add(candidate)
    session.commit()
    return candidate


def _rows(session: Session, candidate_id: int) -> list[CompanyResearch]:
    session.expire_all()
    return list(
        session.execute(
            select(CompanyResearch)
            .where(CompanyResearch.candidate_id == candidate_id)
            .order_by(CompanyResearch.id)
        ).scalars()
    )


def test_successful_fetch_is_persisted_with_provenance(db_session: Session) -> None:
    candidate = _candidate(db_session)
    discovered_at = candidate.discovered_at
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})

    run = run_research(db_session, fetcher, candidate.id, now=T0)

    assert fetcher.calls == [URL]
    assert run.reused is False
    assert run.status == "available"
    row = run.attempt
    assert row.id is not None
    assert row.outcome == "success"
    assert row.failure_category is None
    assert row.requested_url == URL
    assert row.final_url == "https://acme.com/en"
    assert row.fetched_at == T0
    assert row.http_status == 200
    assert row.content_type == "text/html; charset=utf-8"
    assert row.page_title == "Acme Widgets"
    assert row.extracted_text == "Widgets that ship"
    assert row.fetcher == "fake"
    assert row.created_at is not None and row.updated_at is not None
    assert run.latest_usable is not None and run.latest_usable.id == row.id

    # Discovery provenance on the candidate is untouched.
    db_session.expire_all()
    stored = db_session.get(CompanyCandidate, candidate.id)
    assert stored is not None
    assert stored.source_url == "https://acme.com/"
    assert stored.provider == "tavily"
    assert stored.discovered_at == discovered_at
    assert stored.description == "Acme builds widgets."


def test_unknown_candidate_raises_without_fetching(db_session: Session) -> None:
    fetcher = FakeWebsiteFetcher()
    with pytest.raises(CandidateNotFoundError):
        run_research(db_session, fetcher, 999_999)
    assert fetcher.calls == []


@pytest.mark.parametrize("website_url", [None, "", "   "])
def test_candidate_without_website_raises_without_fetching(
    db_session: Session, website_url: str | None
) -> None:
    candidate = _candidate(db_session, website_url=website_url, domain=None)
    fetcher = FakeWebsiteFetcher()

    with pytest.raises(CandidateWebsiteMissingError):
        run_research(db_session, fetcher, candidate.id)

    assert fetcher.calls == []
    assert _rows(db_session, candidate.id) == []


@pytest.mark.parametrize(
    ("website_url", "failure"),
    [
        ("http://127.0.0.1/admin", "blocked_destination"),
        ("http://169.254.169.254/latest/meta-data/", "blocked_destination"),
        ("ftp://acme.com/", "disallowed_scheme"),
        ("https://user:secret@acme.com/", "credentials_in_url"),
        ("https://acme.com:8443/", "disallowed_port"),
        ("not a url", "invalid_url"),
    ],
)
def test_invalid_stored_url_is_recorded_without_fetching(
    db_session: Session, website_url: str, failure: str
) -> None:
    candidate = _candidate(db_session, website_url=website_url, domain=None)
    fetcher = FakeWebsiteFetcher()

    run = run_research(db_session, fetcher, candidate.id, now=T0)

    assert fetcher.calls == []
    assert run.attempt.outcome == "failed"
    assert run.attempt.failure_category == failure
    assert run.attempt.requested_url == website_url
    assert run.attempt.final_url is None
    assert run.status == "unavailable"


def test_failure_without_prior_research(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _failed()})

    run = run_research(db_session, fetcher, candidate.id, now=T0)

    assert run.attempt.outcome == "failed"
    assert run.attempt.failure_category == "timeout"
    assert run.attempt.extracted_text is None
    assert run.latest_usable is None
    assert run.status == "unavailable"


def test_failure_after_success_preserves_earlier_research(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    first = run_research(db_session, fetcher, candidate.id, now=T0)

    fetcher.results[URL] = _failed(T0 + timedelta(hours=1), FetchFailure.HTTP_ERROR)
    with db_session_factory() as second_session:
        second = run_research(
            second_session, fetcher, candidate.id, refresh=True, now=T0 + timedelta(hours=1)
        )

    assert second.attempt.outcome == "failed"
    assert second.latest_usable is not None
    assert second.latest_usable.id == first.attempt.id
    assert second.latest_usable.extracted_text == "Widgets that ship"
    assert second.status == "available"
    assert [row.outcome for row in _rows(db_session, candidate.id)] == ["success", "failed"]


def test_partial_content_is_recorded_honestly(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        results={URL: _ok(outcome=FetchOutcome.PARTIAL, content_truncated=True)}
    )

    run = run_research(db_session, fetcher, candidate.id, now=T0)

    assert run.attempt.outcome == "partial"
    assert run.attempt.content_truncated is True
    assert run.status == "partial"
    assert run.latest_usable is not None


def test_empty_page_is_not_usable(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        results={URL: _ok(outcome=FetchOutcome.EMPTY, text=None)}
    )

    run = run_research(db_session, fetcher, candidate.id, now=T0)

    assert run.attempt.outcome == "empty"
    assert run.attempt.extracted_text is None
    assert run.attempt.failure_category is None
    assert run.latest_usable is None
    assert run.status == "unavailable"


def test_fresh_research_is_reused_without_fetching(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    first = run_research(db_session, fetcher, candidate.id, now=T0)

    second = run_research(
        db_session,
        fetcher,
        candidate.id,
        freshness=timedelta(hours=24),
        now=T0 + timedelta(hours=23),
    )

    assert fetcher.calls == [URL]
    assert second.reused is True
    assert second.attempt.id == first.attempt.id
    assert len(_rows(db_session, candidate.id)) == 1


def test_stale_research_is_fetched_again(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    run_research(db_session, fetcher, candidate.id, now=T0)

    fetcher.results[URL] = _ok(T0 + timedelta(hours=25))
    second = run_research(
        db_session, fetcher, candidate.id, now=T0 + timedelta(hours=25)
    )

    assert len(fetcher.calls) == 2
    assert second.reused is False
    assert second.attempt.fetched_at == T0 + timedelta(hours=25)


def test_explicit_refresh_bypasses_freshness(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    run_research(db_session, fetcher, candidate.id, now=T0)

    second = run_research(
        db_session, fetcher, candidate.id, refresh=True, now=T0 + timedelta(minutes=5)
    )

    assert len(fetcher.calls) == 2
    assert second.reused is False


def test_zero_freshness_disables_reuse(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    run_research(db_session, fetcher, candidate.id, freshness=timedelta(0), now=T0)
    run_research(db_session, fetcher, candidate.id, freshness=timedelta(0), now=T0)
    assert len(fetcher.calls) == 2


def test_failed_attempt_does_not_count_as_fresh(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _failed()})
    run_research(db_session, fetcher, candidate.id, now=T0)
    run_research(db_session, fetcher, candidate.id, now=T0 + timedelta(minutes=1))
    assert len(fetcher.calls) == 2


def test_history_is_bounded_and_keeps_latest_usable(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    success = run_research(db_session, fetcher, candidate.id, now=T0)

    for index in range(1, HISTORY_LIMIT + 4):
        at = T0 + timedelta(hours=index)
        fetcher.results[URL] = _failed(at)
        run_research(db_session, fetcher, candidate.id, refresh=True, now=at)

    rows = _rows(db_session, candidate.id)
    assert len(rows) == HISTORY_LIMIT + 1
    assert success.attempt.id in {row.id for row in rows}
    assert sum(row.outcome == "failed" for row in rows) == HISTORY_LIMIT


def test_history_pruning_is_per_candidate(db_session: Session) -> None:
    first = _candidate(db_session)
    other = _candidate(db_session, website_url="https://beta.io/", domain="beta.io")
    fetcher = FakeWebsiteFetcher(results={URL: _failed(), "https://beta.io/": _ok()})
    run_research(db_session, fetcher, other.id, now=T0)

    for index in range(HISTORY_LIMIT + 2):
        run_research(db_session, fetcher, first.id, now=T0 + timedelta(minutes=index))

    assert len(_rows(db_session, other.id)) == 1
    assert len(_rows(db_session, first.id)) == HISTORY_LIMIT


def test_persistence_failure_rolls_back_and_keeps_prior_research(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    first = run_research(db_session, fetcher, candidate.id, now=T0)

    def broken_prune(session: Session, candidate_id: int) -> None:
        raise OperationalError("DELETE ...", {}, Exception("connection lost"))

    monkeypatch.setattr(research_service, "_prune_history", broken_prune)
    fetcher.results[URL] = _failed(T0 + timedelta(hours=1))
    with db_session_factory() as session:
        with pytest.raises(ResearchPersistenceError):
            run_research(session, fetcher, candidate.id, refresh=True, now=T0 + timedelta(hours=1))
        # The session is usable again after the rollback.
        assert session.get(CompanyCandidate, candidate.id) is not None

    rows = _rows(db_session, candidate.id)
    assert [row.id for row in rows] == [first.attempt.id]
    assert rows[0].outcome == "success"


def test_failed_result_without_category_is_stored_as_malformed(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: replace(_failed(), failure=None)})
    run = run_research(db_session, fetcher, candidate.id, now=T0)
    assert run.attempt.failure_category == "malformed_response"


def test_enrichment_reads_stored_research_idempotently(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(results={URL: _ok()})
    run_research(db_session, fetcher, candidate.id, now=T0)
    fetcher.results[URL] = _failed(T0 + timedelta(hours=1))
    run_research(db_session, fetcher, candidate.id, refresh=True, now=T0 + timedelta(hours=1))
    calls_before = list(fetcher.calls)
    rows_before = len(_rows(db_session, candidate.id))

    first = get_enrichment(db_session, candidate.id)
    second = get_enrichment(db_session, candidate.id)

    assert first == second
    assert fetcher.calls == calls_before
    assert len(_rows(db_session, candidate.id)) == rows_before
    assert first.research_status == "available"
    assert first.website is not None and first.website.outcome == "success"
    assert first.last_attempt is not None and first.last_attempt.outcome == "failed"
    assert first.discovery.source_url == "https://acme.com/"


def test_enrichment_for_unknown_candidate_raises(db_session: Session) -> None:
    with pytest.raises(CandidateNotFoundError):
        get_enrichment(db_session, 424242)
