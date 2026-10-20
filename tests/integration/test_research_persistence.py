"""Integration tests for research persistence and enrichment.

Requires ``TEST_DATABASE_URL``; skipped otherwise. Every fetch is served by
the deterministic fake — no network access is performed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.company_candidate import CompanyCandidate
from app.models.research_record import ResearchRecord
from app.services.enrichment import enrich_candidate
from app.services.providers.fake_website import FakeWebsiteFetcher
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
    WebsiteFetchResult,
)
from app.services.research import (
    CandidateNotFoundError,
    ResearchPersistenceError,
    ResearchWebsiteMissingError,
    get_research_history,
    latest_successful_research,
    list_research,
    research_candidate,
    require_candidate,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _candidate(
    session: Session,
    *,
    website_url: str | None = "https://acme.com/",
    name: str = "Acme Corp",
    domain: str | None = "acme.com",
) -> CompanyCandidate:
    candidate = CompanyCandidate(
        name=name,
        website_url=website_url,
        domain=domain,
        description="Widgets.",
        provider="fake",
        source_url=website_url or "https://source.example/acme",
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def _result(
    *,
    requested_url: str = "https://acme.com/",
    final_url: str | None = "https://acme.com/",
    text: str = "Acme builds reliable widgets.",
    title: str | None = "Acme Corp",
    outcome: FetchOutcome = FetchOutcome.SUCCESS,
    failure: FetchFailureCategory | None = None,
    status: int | None = 200,
    truncated: bool = False,
) -> WebsiteFetchResult:
    return WebsiteFetchResult(
        requested_url=requested_url,
        final_url=final_url,
        fetched_at=FIXED_NOW,
        outcome=outcome,
        failure_category=failure,
        status_code=status,
        content_type="text/html",
        page_title=title,
        text=text,
        truncated=truncated,
    )


def _count(session: Session) -> int:
    return len(session.execute(select(ResearchRecord)).scalars().all())


def test_successful_research_persists_provenance(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={
            "https://acme.com/": _result(
                final_url="https://www.acme.com/about"
            )
        }
    )

    response = research_candidate(db_session, candidate, fetcher)

    assert response.created is True
    assert response.company_candidate_id == candidate.id
    assert response.requested_url == "https://acme.com/"
    assert response.final_url == "https://www.acme.com/about"
    assert response.fetched_at == FIXED_NOW
    assert response.http_status == 200
    assert response.content_type == "text/html"
    assert response.page_title == "Acme Corp"
    assert response.page_text == "Acme builds reliable widgets."
    assert response.outcome is FetchOutcome.SUCCESS
    assert response.failure_category is None
    assert response.truncated is False

    record = db_session.execute(select(ResearchRecord)).scalar_one()
    assert len(record.attempt_key) == 64
    assert record.content_hash is not None
    assert len(record.content_hash) == 64

    db_session.refresh(candidate)
    assert candidate.name == "Acme Corp"
    assert candidate.website_url == "https://acme.com/"
    assert candidate.domain == "acme.com"
    assert candidate.source_url == "https://acme.com/"


def test_missing_candidate_is_reported(db_session: Session) -> None:
    with pytest.raises(CandidateNotFoundError):
        require_candidate(db_session, 999_999)


def test_missing_website_url_is_rejected_before_fetch(
    db_session: Session,
) -> None:
    candidate = _candidate(
        db_session,
        website_url=None,
        domain="no-site.example",
    )
    fetcher = FakeWebsiteFetcher()

    with pytest.raises(ResearchWebsiteMissingError):
        research_candidate(db_session, candidate, fetcher)

    assert fetcher.calls == []
    assert _count(db_session) == 0


def test_fetch_failure_without_prior_research_is_stored(
    db_session: Session,
) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={
            "https://acme.com/": _result(
                outcome=FetchOutcome.FAILED,
                failure=FetchFailureCategory.TIMEOUT,
                text="",
                title=None,
                status=None,
                final_url=None,
            )
        }
    )

    response = research_candidate(db_session, candidate, fetcher)

    assert response.created is True
    assert response.outcome is FetchOutcome.FAILED
    assert response.failure_category is FetchFailureCategory.TIMEOUT
    assert response.page_title is None
    assert response.page_text is None
    assert latest_successful_research(db_session, candidate.id) is None
    assert enrich_candidate(db_session, candidate.id).status == "failed"


def test_failure_after_success_preserves_prior_research(
    db_session: Session,
) -> None:
    candidate = _candidate(db_session)
    success = _result(text="Verified public content.")
    failure = _result(
        outcome=FetchOutcome.FAILED,
        failure=FetchFailureCategory.HTTP_ERROR,
        text="",
        title=None,
        status=503,
    )
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": success}
    )
    first = research_candidate(db_session, candidate, fetcher)

    fetcher.by_url["https://acme.com/"] = failure
    second = research_candidate(db_session, candidate, fetcher)

    assert first.id != second.id
    assert second.outcome is FetchOutcome.FAILED
    assert _count(db_session) == 2

    latest = latest_successful_research(db_session, candidate.id)
    assert latest is not None
    assert latest.id == first.id
    assert latest.page_text == "Verified public content."

    history = get_research_history(db_session, candidate.id)
    assert [record.id for record in history.records] == [second.id, first.id]
    assert history.latest_successful is not None
    assert history.latest_successful.id == first.id

    enrichment = enrich_candidate(db_session, candidate.id)
    assert enrichment.status == "stale"
    assert enrichment.research is not None
    assert enrichment.research.id == first.id
    assert enrichment.latest_attempt is not None
    assert enrichment.latest_attempt.id == second.id


def test_partial_content_is_persisted(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={
            "https://acme.com/": _result(
                outcome=FetchOutcome.PARTIAL,
                text="Short visible text.",
                truncated=True,
            )
        }
    )

    response = research_candidate(db_session, candidate, fetcher)

    assert response.outcome is FetchOutcome.PARTIAL
    assert response.truncated is True
    assert response.page_text == "Short visible text."
    assert enrich_candidate(db_session, candidate.id).status == "partial"


def test_repeated_identical_attempt_is_idempotent(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": _result()}
    )

    first = research_candidate(db_session, candidate, fetcher)
    second = research_candidate(db_session, candidate, fetcher)

    assert first.created is True
    assert second.created is False
    assert second.id == first.id
    assert _count(db_session) == 1
    # An explicit research request may fetch again, but must not duplicate
    # the stored record.
    assert len(fetcher.calls) == 2


def test_changed_content_creates_a_new_record(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": _result(text="Version one.")}
    )
    first = research_candidate(db_session, candidate, fetcher)

    fetcher.by_url["https://acme.com/"] = _result(text="Version two.")
    second = research_candidate(db_session, candidate, fetcher)

    assert second.created is True
    assert second.id != first.id
    assert _count(db_session) == 2
    latest = latest_successful_research(db_session, candidate.id)
    assert latest is not None
    assert latest.page_text == "Version two."


def test_success_after_failure_creates_a_new_record(
    db_session: Session,
) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={
            "https://acme.com/": _result(
                outcome=FetchOutcome.FAILED,
                failure=FetchFailureCategory.CONNECTION_ERROR,
                text="",
                title=None,
                status=None,
            )
        }
    )
    failure = research_candidate(db_session, candidate, fetcher)

    fetcher.by_url["https://acme.com/"] = _result()
    success = research_candidate(db_session, candidate, fetcher)

    assert success.id != failure.id
    assert _count(db_session) == 2
    enrichment = enrich_candidate(db_session, candidate.id)
    assert enrichment.status == "researched"
    assert enrichment.research is not None
    assert enrichment.research.id == success.id


def test_repeated_identical_failure_is_idempotent(
    db_session: Session,
) -> None:
    candidate = _candidate(db_session)
    failure = _result(
        outcome=FetchOutcome.FAILED,
        failure=FetchFailureCategory.DNS_FAILURE,
        text="",
        title=None,
        status=None,
    )
    fetcher = FakeWebsiteFetcher(by_url={"https://acme.com/": failure})

    first = research_candidate(db_session, candidate, fetcher)
    second = research_candidate(db_session, candidate, fetcher)

    assert second.id == first.id
    assert second.created is False
    assert _count(db_session) == 1


def test_persistence_failure_rolls_back(db_session: Session) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": _result()}
    )

    with patch.object(
        db_session,
        "commit",
        side_effect=SQLAlchemyError("simulated database failure"),
    ):
        with pytest.raises(ResearchPersistenceError):
            research_candidate(db_session, candidate, fetcher)

    assert _count(db_session) == 0
    assert list_research(db_session, candidate.id) == []


def test_enrichment_is_idempotent_and_does_not_fetch(
    db_session: Session,
) -> None:
    candidate = _candidate(db_session)
    fetcher = FakeWebsiteFetcher(
        by_url={"https://acme.com/": _result()}
    )
    research_candidate(db_session, candidate, fetcher)

    first = enrich_candidate(db_session, candidate.id)
    second = enrich_candidate(db_session, candidate.id)

    assert first.model_dump() == second.model_dump()
    assert len(fetcher.calls) == 1
    assert _count(db_session) == 1
    assert first.candidate.source_url == "https://acme.com/"


def test_enrichment_reports_not_researched(db_session: Session) -> None:
    candidate = _candidate(db_session)

    enrichment = enrich_candidate(db_session, candidate.id)

    assert enrichment.status == "not_researched"
    assert enrichment.research is None
    assert enrichment.latest_attempt is None
