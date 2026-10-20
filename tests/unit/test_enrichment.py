"""Unit tests for the pure enrichment projection and attempt keys."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.company_candidate import CompanyCandidate
from app.models.research_record import ResearchRecord
from app.services.enrichment import build_enrichment
from app.services.providers.website import (
    FetchFailureCategory,
    FetchOutcome,
)
from app.services.research import build_attempt_key

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _candidate() -> CompanyCandidate:
    return CompanyCandidate(
        id=7,
        name="Acme Corp",
        website_url="https://acme.com/",
        domain="acme.com",
        description="Widgets.",
        provider="fake",
        provider_result_id=None,
        source_url="https://search.example/acme",
        discovered_at=NOW,
        updated_at=NOW,
    )


def _record(
    record_id: int,
    outcome: FetchOutcome,
    *,
    text: str | None = "Visible text.",
    failure: FetchFailureCategory | None = None,
) -> ResearchRecord:
    return ResearchRecord(
        id=record_id,
        company_candidate_id=7,
        requested_url="https://acme.com/",
        final_url="https://acme.com/" if outcome != FetchOutcome.FAILED else None,
        fetched_at=NOW,
        http_status=200 if outcome != FetchOutcome.FAILED else None,
        content_type="text/html" if outcome != FetchOutcome.FAILED else None,
        page_title="Acme" if outcome != FetchOutcome.FAILED else None,
        page_text=text if outcome != FetchOutcome.FAILED else None,
        outcome=outcome.value,
        failure_category=failure.value if failure is not None else None,
        truncated=False,
        content_hash="a" * 64 if outcome != FetchOutcome.FAILED else None,
        attempt_key=f"key-{record_id}",
        created_at=NOW,
        updated_at=NOW,
    )


def test_not_researched_when_no_records() -> None:
    enrichment = build_enrichment(_candidate(), [])

    assert enrichment.status == "not_researched"
    assert enrichment.research is None
    assert enrichment.latest_attempt is None


def test_failed_without_success() -> None:
    failure = _record(
        1, FetchOutcome.FAILED, failure=FetchFailureCategory.TIMEOUT
    )

    enrichment = build_enrichment(_candidate(), [failure])

    assert enrichment.status == "failed"
    assert enrichment.research is None
    assert enrichment.latest_attempt is not None
    assert enrichment.latest_attempt.id == 1


def test_success_and_partial_statuses() -> None:
    success = _record(2, FetchOutcome.SUCCESS)
    partial = _record(3, FetchOutcome.PARTIAL)

    assert build_enrichment(_candidate(), [success]).status == "researched"
    assert build_enrichment(_candidate(), [partial]).status == "partial"


def test_stale_reports_failure_without_hiding_success() -> None:
    success = _record(1, FetchOutcome.SUCCESS, text="Verified.")
    failure = _record(
        2, FetchOutcome.FAILED, failure=FetchFailureCategory.HTTP_ERROR
    )

    enrichment = build_enrichment(_candidate(), [failure, success])

    assert enrichment.status == "stale"
    assert enrichment.research is not None
    assert enrichment.research.id == 1
    assert enrichment.research.page_text == "Verified."
    assert enrichment.latest_attempt is not None
    assert enrichment.latest_attempt.id == 2


def test_candidate_provenance_is_preserved() -> None:
    enrichment = build_enrichment(_candidate(), [])

    assert enrichment.candidate.source_url == "https://search.example/acme"
    assert enrichment.candidate.provider == "fake"
    assert enrichment.candidate.website_url == "https://acme.com/"


def test_projection_is_idempotent() -> None:
    records = [
        _record(2, FetchOutcome.SUCCESS),
        _record(
            1, FetchOutcome.FAILED, failure=FetchFailureCategory.TIMEOUT
        ),
    ]

    first = build_enrichment(_candidate(), records)
    second = build_enrichment(_candidate(), records)

    assert first.model_dump() == second.model_dump()


def test_attempt_key_is_deterministic_and_chained() -> None:
    signature = "a" * 64

    first = build_attempt_key(7, None, signature)
    repeated = build_attempt_key(7, None, signature)
    after_success = build_attempt_key(7, 1, signature)
    changed_content = build_attempt_key(7, 1, "b" * 64)

    assert first == repeated
    assert len(first) == 64
    assert first != after_success
    assert after_success != changed_content
