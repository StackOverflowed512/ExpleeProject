"""Integration tests for the qualification and prospect services (PostgreSQL).

Requires ``TEST_DATABASE_URL``; skipped otherwise. No network access: rows
are created directly or through fake providers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.company_research import CompanyResearch
from app.models.saved_prospect import SavedProspect
from app.schemas.prospects import ProspectQuery
from app.schemas.qualification import QualificationCriteria
from app.services import prospects as prospects_service
from app.services.prospects import (
    ProspectNotFoundError,
    ProspectNotQualifiedError,
    QualificationNotFoundError,
    QualificationRequiredError,
    get_prospect,
    list_prospects,
    save_prospect,
)
from app.services.qualification import (
    QualificationPersistenceError,
    list_qualifications,
    run_qualification,
    to_qualification_record,
)
from app.services.qualification_rules import RULE_VERSION
from app.services.research import CandidateNotFoundError

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
SAAS = QualificationCriteria(target_keywords=["saas"])


def _candidate(
    session: Session,
    *,
    name: str = "Acme Corp",
    domain: str = "acme.com",
    description: str | None = "Acme builds SaaS for logistics.",
) -> CompanyCandidate:
    candidate = CompanyCandidate(
        name=name,
        website_url=f"https://{domain}/",
        domain=domain,
        description=description,
        provider="fake",
        source_url=f"https://{domain}/",
    )
    session.add(candidate)
    session.commit()
    return candidate


def _research(
    session: Session, candidate: CompanyCandidate, text_: str, *, outcome: str = "success"
) -> CompanyResearch:
    row = CompanyResearch(
        candidate_id=candidate.id,
        requested_url=candidate.website_url,
        final_url=candidate.website_url,
        fetched_at=T0,
        fetcher="fake",
        outcome=outcome,
        failure_category="timeout" if outcome == "failed" else None,
        extracted_text=text_ if outcome in ("success", "partial") else None,
        content_truncated=outcome == "partial",
    )
    session.add(row)
    session.commit()
    return row


def _qualified(session: Session, candidate: CompanyCandidate) -> CompanyQualification:
    """Qualify with industry fit confirmed by (complete) website research."""
    _research(session, candidate, "Acme builds SaaS for logistics teams.")
    row = run_qualification(session, candidate.id, SAAS, now=T0)
    assert row.status == "qualified"
    return row


# -- qualification ------------------------------------------------------------
def test_qualification_run_is_persisted_with_audit_fields(db_session: Session) -> None:
    candidate = _candidate(db_session)
    research = _research(db_session, candidate, "B2B SaaS for logistics in Berlin.")
    criteria = QualificationCriteria(
        profile="berlin-saas",
        target_keywords=["SaaS"],
        target_locations=["Berlin"],
        excluded_keywords=["casino"],
        threshold=70,
    )

    row = run_qualification(db_session, candidate.id, criteria, now=T0)

    db_session.expire_all()
    stored = db_session.get(CompanyQualification, row.id)
    assert stored is not None
    assert stored.candidate_id == candidate.id
    assert stored.research_id == research.id
    assert stored.rule_version == RULE_VERSION
    assert stored.criteria == {
        "profile": "berlin-saas",
        "target_keywords": ["saas"],
        "target_locations": ["berlin"],
        "target_tlds": [],
        "excluded_keywords": ["casino"],
        "threshold": 70,
        "min_evidence_coverage": 0.5,
    }
    assert stored.status == "qualified"
    assert stored.score == 100
    assert stored.threshold == 70
    assert stored.evidence_coverage == pytest.approx(1.0)
    assert [f["key"] for f in stored.factors] == ["industry_fit", "location_fit", "exclusion_check"]
    assert stored.rationale.startswith("Qualified: score 100 meets the threshold 70.")
    assert stored.qualified_at == T0
    assert stored.created_at is not None

    record = to_qualification_record(stored)
    assert record.matched_signals == ["saas", "berlin"]
    assert record.missing_signals == []
    assert record.disqualifying_signals == []


def test_qualification_does_not_modify_candidate_or_research(db_session: Session) -> None:
    candidate = _candidate(db_session)
    research = _research(db_session, candidate, "SaaS")
    before = (candidate.source_url, candidate.description, candidate.updated_at)

    run_qualification(db_session, candidate.id, SAAS, now=T0)

    db_session.expire_all()
    stored = db_session.get(CompanyCandidate, candidate.id)
    assert stored is not None
    assert (stored.source_url, stored.description, stored.updated_at) == before
    stored_research = db_session.get(CompanyResearch, research.id)
    assert stored_research is not None and stored_research.extracted_text == "SaaS"


def test_rerunning_appends_history_without_overwriting(db_session: Session) -> None:
    candidate = _candidate(db_session, description="A company.")
    first = run_qualification(db_session, candidate.id, SAAS, now=T0)
    assert first.status == "insufficient_evidence"

    _research(db_session, candidate, "We sell SaaS.")
    second = run_qualification(db_session, candidate.id, SAAS, now=T0 + timedelta(hours=1))

    total, rows = list_qualifications(db_session, candidate.id, limit=10)
    assert total == 2
    assert [r.id for r in rows] == [second.id, first.id]
    db_session.expire_all()
    original = db_session.get(CompanyQualification, first.id)
    assert original is not None
    assert original.status == "insufficient_evidence"
    assert original.research_id is None
    assert rows[0].status == "qualified"


def test_identical_inputs_are_repeatable(db_session: Session) -> None:
    candidate = _candidate(db_session)
    _research(db_session, candidate, "B2B SaaS.")
    first = run_qualification(db_session, candidate.id, SAAS, now=T0)
    second = run_qualification(db_session, candidate.id, SAAS, now=T0 + timedelta(minutes=5))

    fields = (
        "rule_version",
        "criteria",
        "status",
        "score",
        "threshold",
        "evidence_coverage",
        "factors",
        "rationale",
        "research_id",
    )
    assert {f: getattr(first, f) for f in fields} == {f: getattr(second, f) for f in fields}
    assert first.id != second.id


def test_qualification_uses_latest_usable_research(db_session: Session) -> None:
    candidate = _candidate(db_session, description=None)
    usable = _research(db_session, candidate, "SaaS platform.")
    _research(db_session, candidate, "", outcome="failed")

    row = run_qualification(db_session, candidate.id, SAAS, now=T0)
    assert row.research_id == usable.id
    assert row.status == "qualified"


def test_qualification_without_any_research(db_session: Session) -> None:
    candidate = _candidate(db_session, description=None)
    row = run_qualification(db_session, candidate.id, SAAS, now=T0)
    assert row.status == "insufficient_evidence"
    assert row.research_id is None


def test_unknown_candidate_cannot_be_qualified(db_session: Session) -> None:
    with pytest.raises(CandidateNotFoundError):
        run_qualification(db_session, 999_999, SAAS)
    with pytest.raises(CandidateNotFoundError):
        list_qualifications(db_session, 999_999, limit=5)


def test_qualification_persistence_failure_rolls_back(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(db_session)

    def broken_commit() -> None:
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(db_session, "commit", broken_commit)
    with pytest.raises(QualificationPersistenceError):
        run_qualification(db_session, candidate.id, SAAS)
    monkeypatch.undo()

    count = db_session.execute(text("SELECT count(*) FROM company_qualifications")).scalar_one()
    assert count == 0


# -- saving prospects -----------------------------------------------------------
def test_save_requires_a_qualification(db_session: Session) -> None:
    candidate = _candidate(db_session)
    with pytest.raises(QualificationRequiredError):
        save_prospect(db_session, candidate.id)
    assert db_session.execute(select(SavedProspect)).first() is None


@pytest.mark.parametrize(
    ("description", "status"),
    [("We make furniture.", "insufficient_evidence"), ("Online casino SaaS.", "unqualified")],
)
def test_save_rejects_non_qualified_results(
    db_session: Session, description: str, status: str
) -> None:
    candidate = _candidate(db_session, description=description)
    criteria = QualificationCriteria(target_keywords=["saas"], excluded_keywords=["casino"])
    row = run_qualification(db_session, candidate.id, criteria)
    assert row.status == status

    with pytest.raises(ProspectNotQualifiedError):
        save_prospect(db_session, candidate.id)
    assert db_session.execute(select(SavedProspect)).first() is None


def test_save_rejects_score_below_threshold(db_session: Session) -> None:
    candidate = _candidate(db_session, description="Furniture maker.")
    _research(db_session, candidate, "Furniture from Lisbon.")
    row = run_qualification(
        db_session, candidate.id, QualificationCriteria(target_keywords=["saas"])
    )
    assert (row.status, row.score) == ("unqualified", 0)
    with pytest.raises(ProspectNotQualifiedError):
        save_prospect(db_session, candidate.id)


def test_backend_rechecks_score_against_recorded_threshold(db_session: Session) -> None:
    candidate = _candidate(db_session)
    row = _qualified(db_session, candidate)
    # A tampered row claiming 'qualified' below its threshold is still refused.
    db_session.execute(
        update(CompanyQualification).where(CompanyQualification.id == row.id).values(score=10)
    )
    db_session.commit()
    with pytest.raises(ProspectNotQualifiedError):
        save_prospect(db_session, candidate.id)


def test_save_after_successful_qualification(db_session: Session) -> None:
    candidate = _candidate(db_session)
    qualification = _qualified(db_session, candidate)

    result = save_prospect(db_session, candidate.id)

    assert result.created is True
    assert result.prospect.candidate_id == candidate.id
    assert result.prospect.qualification_id == qualification.id
    assert result.prospect.saved_at is not None
    assert result.qualification.id == qualification.id


def test_duplicate_save_returns_existing_prospect_unchanged(db_session: Session) -> None:
    candidate = _candidate(db_session)
    first_qualification = _qualified(db_session, candidate)
    first = save_prospect(db_session, candidate.id)
    newer = run_qualification(db_session, candidate.id, SAAS, now=T0 + timedelta(days=1))

    second = save_prospect(db_session, candidate.id, qualification_id=newer.id)

    assert second.created is False
    assert second.prospect.id == first.prospect.id
    assert second.prospect.qualification_id == first_qualification.id
    assert db_session.execute(text("SELECT count(*) FROM saved_prospects")).scalar_one() == 1


def test_concurrent_insert_conflict_resolves_to_existing_row(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a race: another transaction saves after our existence check."""
    candidate = _candidate(db_session)
    qualification = _qualified(db_session, candidate)
    with db_session_factory() as other:
        other.add(SavedProspect(candidate_id=candidate.id, qualification_id=qualification.id))
        other.commit()

    original = prospects_service._existing
    calls = {"n": 0}

    def stale_first_check(session: Session, candidate_id: int):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return None if calls["n"] == 1 else original(session, candidate_id)

    monkeypatch.setattr(prospects_service, "_existing", stale_first_check)
    result = save_prospect(db_session, candidate.id)

    assert result.created is False
    assert db_session.execute(text("SELECT count(*) FROM saved_prospects")).scalar_one() == 1


def test_database_rejects_duplicate_prospect_rows(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    candidate = _candidate(db_session)
    qualification = _qualified(db_session, candidate)
    save_prospect(db_session, candidate.id)

    with db_session_factory() as other:
        other.add(SavedProspect(candidate_id=candidate.id, qualification_id=qualification.id))
        with pytest.raises(IntegrityError):
            other.commit()


def test_explicit_qualification_must_belong_to_candidate(db_session: Session) -> None:
    acme = _candidate(db_session)
    beta = _candidate(db_session, name="Beta", domain="beta.io")
    beta_qualification = _qualified(db_session, beta)

    with pytest.raises(QualificationNotFoundError):
        save_prospect(db_session, acme.id, qualification_id=beta_qualification.id)
    with pytest.raises(QualificationNotFoundError):
        save_prospect(db_session, acme.id, qualification_id=987_654)


def test_explicit_older_qualified_run_can_justify_save(db_session: Session) -> None:
    candidate = _candidate(db_session)
    qualified = _qualified(db_session, candidate)
    latest = run_qualification(
        db_session,
        candidate.id,
        QualificationCriteria(target_keywords=["saas"], excluded_keywords=["logistics"]),
        now=T0 + timedelta(hours=1),
    )
    assert latest.status == "unqualified"

    with pytest.raises(ProspectNotQualifiedError):
        save_prospect(db_session, candidate.id)
    result = save_prospect(db_session, candidate.id, qualification_id=qualified.id)
    assert result.created is True
    assert result.prospect.qualification_id == qualified.id


def test_unknown_candidate_cannot_be_saved(db_session: Session) -> None:
    with pytest.raises(CandidateNotFoundError):
        save_prospect(db_session, 123_456)


def test_saving_keeps_discovery_and_research_history(db_session: Session) -> None:
    candidate = _candidate(db_session)
    _research(db_session, candidate, "SaaS")
    _research(db_session, candidate, "", outcome="failed")
    _qualified(db_session, candidate)
    count_research = text("SELECT count(*) FROM company_research")
    before = db_session.execute(count_research).scalar_one()
    save_prospect(db_session, candidate.id)

    db_session.expire_all()
    assert db_session.execute(count_research).scalar_one() == before == 3
    stored = db_session.get(CompanyCandidate, candidate.id)
    assert stored is not None and stored.source_url == "https://acme.com/"


# -- search -------------------------------------------------------------------------
def _saved(
    session: Session, name: str, domain: str, description: str, *, saved_at: datetime
) -> SavedProspect:
    candidate = _candidate(session, name=name, domain=domain, description=description)
    _research(session, candidate, description)
    criteria = QualificationCriteria(
        target_keywords=["saas"], target_locations=["berlin"], threshold=50
    )
    run_qualification(session, candidate.id, criteria, now=T0)
    prospect = save_prospect(session, candidate.id).prospect
    session.execute(
        update(SavedProspect).where(SavedProspect.id == prospect.id).values(saved_at=saved_at)
    )
    session.commit()
    return prospect


@pytest.fixture
def three_prospects(db_session: Session) -> list[SavedProspect]:
    return [
        _saved(db_session, "Gamma Analytics", "gamma.io", "SaaS analytics in Berlin.", saved_at=T0),
        _saved(
            db_session,
            "alpha_ops",
            "alpha.dev",
            "SaaS ops tools from Lisbon.",
            saved_at=T0 + timedelta(hours=1),
        ),
        _saved(
            db_session,
            "Beta 100% Cloud",
            "beta.com",
            "SaaS cloud in Berlin.",
            saved_at=T0 + timedelta(hours=1),
        ),
    ]


def _ids(session: Session, **query: object) -> list[int]:
    _, rows = list_prospects(session, ProspectQuery.model_validate(query))
    return [prospect.id for prospect, _, _ in rows]


def test_default_ordering_is_newest_first_with_id_tiebreak(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    gamma, alpha, beta = three_prospects
    assert _ids(db_session) == [beta.id, alpha.id, gamma.id]
    assert _ids(db_session, sort="saved_at_asc") == [gamma.id, alpha.id, beta.id]
    assert _ids(db_session, sort="name_asc") == [alpha.id, beta.id, gamma.id]
    # Berlin ones score 100, Lisbon 63; ties broken by saved_at then ID.
    assert _ids(db_session, sort="score_desc") == [beta.id, gamma.id, alpha.id]


def test_ordering_is_stable_across_calls(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    for sort in ("saved_at_desc", "saved_at_asc", "score_desc", "name_asc"):
        assert _ids(db_session, sort=sort) == _ids(db_session, sort=sort)


def test_search_matches_name_domain_and_description(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    gamma, alpha, beta = three_prospects
    assert _ids(db_session, q="gamma") == [gamma.id]
    assert _ids(db_session, q="ALPHA.DEV") == [alpha.id]
    assert _ids(db_session, q="lisbon") == [alpha.id]
    assert _ids(db_session, q="no-such-company") == []


def test_search_escapes_like_wildcards(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    gamma, alpha, beta = three_prospects
    assert _ids(db_session, q="100%") == [beta.id]
    assert _ids(db_session, q="a_o") == [alpha.id]
    assert _ids(db_session, q="%%") == []


def test_filters_by_domain_score_and_saved_window(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    gamma, alpha, beta = three_prospects
    assert _ids(db_session, domain="GAMMA.IO") == [gamma.id]
    assert _ids(db_session, min_score=90) == [beta.id, gamma.id]
    assert _ids(db_session, max_score=70) == [alpha.id]
    assert _ids(db_session, min_score=63, max_score=63) == [alpha.id]
    assert _ids(db_session, saved_after=T0 + timedelta(minutes=30)) == [beta.id, alpha.id]
    assert _ids(db_session, saved_before=T0 + timedelta(minutes=30)) == [gamma.id]


def test_pagination_is_bounded_and_counts_total(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    gamma, alpha, beta = three_prospects
    total, rows = list_prospects(db_session, ProspectQuery(limit=2))
    assert total == 3
    assert [p.id for p, _, _ in rows] == [beta.id, alpha.id]
    total, rows = list_prospects(db_session, ProspectQuery(limit=2, offset=2))
    assert total == 3
    assert [p.id for p, _, _ in rows] == [gamma.id]
    total, rows = list_prospects(db_session, ProspectQuery(offset=50))
    assert (total, rows) == (3, [])


def test_get_prospect_and_missing_prospect(
    db_session: Session, three_prospects: list[SavedProspect]
) -> None:
    prospect, candidate, qualification = get_prospect(db_session, three_prospects[0].id)
    assert candidate.name == "Gamma Analytics"
    assert qualification.id == prospect.qualification_id
    with pytest.raises(ProspectNotFoundError):
        get_prospect(db_session, 555_555)


# -- v2: preliminary results, rule versions, signal lists ---------------------------
def test_preliminary_result_cannot_be_saved_until_website_confirms(db_session: Session) -> None:
    candidate = _candidate(
        db_session,
        name="Best Software Companies in Berlin | Directory",
        description="Top SaaS startups.",
    )
    preliminary = run_qualification(db_session, candidate.id, SAAS, now=T0)
    assert preliminary.status == "preliminary"
    with pytest.raises(ProspectNotQualifiedError, match="research the company's website"):
        save_prospect(db_session, candidate.id)

    _research(db_session, candidate, "Acme is a SaaS platform for freight.")
    confirmed = run_qualification(db_session, candidate.id, SAAS, now=T0 + timedelta(hours=1))
    assert confirmed.status == "qualified"
    result = save_prospect(db_session, candidate.id)
    assert result.prospect.qualification_id == confirmed.id

    stored = db_session.get(CompanyQualification, preliminary.id)
    assert stored is not None and stored.status == "preliminary"  # history kept


def test_refuting_website_research_records_unconfirmed_signals(db_session: Session) -> None:
    candidate = _candidate(db_session, name="Top SaaS Tools 2027", description=None)
    _research(db_session, candidate, "Handmade ceramics from Porto.")
    row = run_qualification(db_session, candidate.id, SAAS, now=T0)

    record = to_qualification_record(row)
    industry = record.factors[0]
    assert record.status == "unqualified"
    assert industry.signals_unconfirmed == ["saas"]
    assert industry.evidence[0].counted is False
    assert record.matched_signals == []


def test_v1_qualified_run_cannot_justify_a_new_save(db_session: Session) -> None:
    candidate = _candidate(db_session)
    v1 = CompanyQualification(
        candidate_id=candidate.id,
        rule_version="qualification-rules/v1",
        # Stored v1 criteria may break today's validation (target == exclusion).
        criteria={
            "profile": "legacy",
            "target_keywords": ["saas"],
            "target_locations": [],
            "target_tlds": [],
            "excluded_keywords": ["saas"],
            "threshold": 60,
            "min_evidence_coverage": 0.5,
        },
        status="qualified",
        score=100,
        threshold=60,
        evidence_coverage=1.0,
        factors=[],
        rationale="Qualified under v1.",
        qualified_at=T0,
    )
    db_session.add(v1)
    db_session.commit()

    with pytest.raises(ProspectNotQualifiedError, match="re-run qualification"):
        save_prospect(db_session, candidate.id, qualification_id=v1.id)
    # The legacy row is still readable through history.
    total, rows = list_qualifications(db_session, candidate.id, limit=5)
    record = to_qualification_record(rows[0])
    assert total == 1
    assert record.rule_version == "qualification-rules/v1"
    assert record.criteria.excluded_keywords == ["saas"]


def test_signal_lists_are_deduplicated(db_session: Session) -> None:
    candidate = _candidate(db_session, description="Berlin SaaS.")
    _research(db_session, candidate, "SaaS made in Berlin.")
    criteria = QualificationCriteria(
        target_keywords=["saas", "berlin"], target_locations=["berlin", "munich"]
    )
    record = to_qualification_record(run_qualification(db_session, candidate.id, criteria))
    assert record.matched_signals == ["berlin", "saas"]
    assert record.unconfirmed_signals == []
    assert record.missing_signals == ["munich"]


def test_unconfirmed_signals_are_aggregated_once(db_session: Session) -> None:
    # "berlin" is both a keyword and a location; both factors see it only in
    # the listicle title, and the complete website page refutes it.
    candidate = _candidate(
        db_session, name="Best Berlin SaaS Companies", description="Berlin SaaS list."
    )
    _research(db_session, candidate, "Handmade ceramics from Porto.")
    criteria = QualificationCriteria(
        target_keywords=["berlin", "saas"], target_locations=["berlin", "munich"]
    )
    record = to_qualification_record(run_qualification(db_session, candidate.id, criteria))

    assert record.status == "unqualified"
    assert record.matched_signals == []
    assert record.unconfirmed_signals == ["berlin", "saas"]
    assert record.missing_signals == ["munich"]
    every = record.matched_signals + record.unconfirmed_signals + record.missing_signals
    assert len(every) == len(set(every))


def test_disqualifying_signals_are_aggregated_once(db_session: Session) -> None:
    candidate = _candidate(db_session, name="Casino Casino", description="casino")
    _research(db_session, candidate, "SaaS for the casino industry. Casino casino.")
    criteria = QualificationCriteria(target_keywords=["saas"], excluded_keywords=["casino"])
    record = to_qualification_record(run_qualification(db_session, candidate.id, criteria))
    assert record.status == "unqualified"
    assert record.disqualifying_signals == ["casino"]
    assert record.matched_signals == ["saas"]


def test_persisted_v2_factors_include_evidence_levels(db_session: Session) -> None:
    candidate = _candidate(db_session)
    row = _qualified(db_session, candidate)
    db_session.expire_all()
    stored = db_session.get(CompanyQualification, row.id)
    assert stored is not None
    assert stored.rule_version == "qualification-rules/v2"
    industry = stored.factors[0]
    assert industry["evidence_level"] == "website"
    assert industry["evidence"][0]["level"] == "website"
    assert industry["evidence"][0]["counted"] is True
