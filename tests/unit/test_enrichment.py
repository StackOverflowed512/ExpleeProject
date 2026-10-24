"""Unit tests for enrichment and research status logic. No database access."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.company_candidate import CompanyCandidate
from app.models.company_research import CompanyResearch
from app.services.enrichment import build_enrichment
from app.services.research import research_status

DISCOVERED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


def _candidate(**overrides: object) -> CompanyCandidate:
    values: dict[str, object] = {
        "id": 7,
        "name": "Acme Corp",
        "website_url": "https://acme.com/",
        "domain": "acme.com",
        "description": "Acme builds widgets.",
        "provider": "tavily",
        "provider_result_id": "r-1",
        "source_url": "https://acme.com/",
        "discovered_at": DISCOVERED,
        "updated_at": DISCOVERED,
    }
    values.update(overrides)
    return CompanyCandidate(**values)


def _research(row_id: int, outcome: str, **overrides: object) -> CompanyResearch:
    values: dict[str, object] = {
        "id": row_id,
        "candidate_id": 7,
        "requested_url": "https://acme.com/",
        "final_url": "https://acme.com/en",
        "fetched_at": FETCHED,
        "fetcher": "fake",
        "outcome": outcome,
        "failure_category": "timeout" if outcome == "failed" else None,
        "http_status": 200 if outcome != "failed" else None,
        "content_type": "text/html" if outcome != "failed" else None,
        "page_title": "Acme" if outcome != "failed" else None,
        "extracted_text": "Widgets that ship" if outcome in ("success", "partial") else None,
        "content_truncated": outcome == "partial",
        "created_at": FETCHED,
        "updated_at": FETCHED,
    }
    values.update(overrides)
    return CompanyResearch(**values)


def test_status_mapping() -> None:
    success = _research(1, "success")
    partial = _research(2, "partial")
    failed = _research(3, "failed")
    empty = _research(4, "empty")

    assert research_status(None, None) == "not_researched"
    assert research_status(None, failed) == "unavailable"
    assert research_status(None, empty) == "unavailable"
    assert research_status(success, failed) == "available"
    assert research_status(partial, partial) == "partial"


def test_enrichment_with_successful_research() -> None:
    usable = _research(1, "success")
    enriched = build_enrichment(_candidate(), usable, usable)

    assert enriched.research_status == "available"
    assert enriched.name == "Acme Corp"
    assert enriched.description == "Acme builds widgets."
    assert enriched.discovery.provider == "tavily"
    assert enriched.discovery.source_url == "https://acme.com/"
    assert enriched.discovery.discovered_at == DISCOVERED
    assert enriched.website is not None
    assert enriched.website.extracted_text == "Widgets that ship"
    assert enriched.website.final_url == "https://acme.com/en"
    assert enriched.website.fetched_at == FETCHED
    assert enriched.last_attempt is not None
    assert enriched.last_attempt.id == 1


def test_enrichment_keeps_success_when_latest_attempt_failed() -> None:
    usable = _research(1, "success")
    failed = _research(2, "failed")
    enriched = build_enrichment(_candidate(), usable, failed)

    assert enriched.research_status == "available"
    assert enriched.website is not None and enriched.website.id == 1
    assert enriched.last_attempt is not None
    assert enriched.last_attempt.outcome == "failed"
    assert enriched.last_attempt.failure_category == "timeout"


def test_enrichment_without_research_leaves_fields_absent() -> None:
    enriched = build_enrichment(
        _candidate(description=None, website_url=None, provider_result_id=None),
        None,
        None,
    )
    assert enriched.research_status == "not_researched"
    assert enriched.website is None
    assert enriched.last_attempt is None
    assert enriched.description is None
    assert enriched.website_url is None
    assert enriched.discovery.provider_result_id is None


def test_enrichment_with_only_failed_research() -> None:
    failed = _research(3, "failed")
    enriched = build_enrichment(_candidate(), None, failed)
    assert enriched.research_status == "unavailable"
    assert enriched.website is None
    assert enriched.last_attempt is not None


def test_enrichment_is_deterministic_for_equal_inputs() -> None:
    first = build_enrichment(_candidate(), _research(1, "partial"), _research(1, "partial"))
    second = build_enrichment(_candidate(), _research(1, "partial"), _research(1, "partial"))
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.research_status == "partial"


def test_enrichment_does_not_modify_inputs() -> None:
    candidate = _candidate()
    usable = _research(1, "success")
    before = (candidate.source_url, candidate.provider, candidate.description)
    build_enrichment(candidate, usable, usable)
    assert (candidate.source_url, candidate.provider, candidate.description) == before
    assert usable.extracted_text == "Widgets that ship"
