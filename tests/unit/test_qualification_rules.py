"""Unit tests for the deterministic qualification rules. No database access."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.qualification import (
    DEFAULT_MIN_EVIDENCE_COVERAGE,
    DEFAULT_PROFILE,
    DEFAULT_TARGET_KEYWORDS,
    DEFAULT_THRESHOLD,
    MAX_TERMS,
    QualificationCriteria,
    QualifyRequest,
)
from app.schemas.research import DiscoveryProvenance, EnrichedCompany, ResearchRecord
from app.services.qualification_rules import (
    EXCLUSION_WEIGHT,
    INDUSTRY_WEIGHT,
    LOCATION_WEIGHT,
    RULE_VERSION,
    evaluate,
)

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def _research(
    text: str | None = "We build B2B SaaS for logistics teams in Berlin.",
    *,
    outcome: str = "success",
    title: str | None = "Acme Logistics Cloud",
) -> ResearchRecord:
    return ResearchRecord(
        id=11,
        candidate_id=7,
        outcome=outcome,  # type: ignore[arg-type]
        failure_category=None,
        requested_url="https://acme.de/",
        final_url="https://acme.de/en",
        http_status=200,
        content_type="text/html",
        page_title=title,
        content_truncated=outcome == "partial",
        fetcher="fake",
        fetched_at=T0,
        created_at=T0,
        updated_at=T0,
        extracted_text=text,
    )


def _company(
    *,
    name: str = "Acme",
    description: str | None = None,
    domain: str | None = "acme.de",
    website: ResearchRecord | None = None,
    website_url: str | None = "https://acme.de/",
) -> EnrichedCompany:
    return EnrichedCompany(
        candidate_id=7,
        name=name,
        website_url=website_url,
        domain=domain,
        description=description,
        discovery=DiscoveryProvenance(
            provider="fake",
            provider_result_id=None,
            source_url="https://acme.de/",
            discovered_at=T0,
        ),
        research_status="available" if website is not None else "not_researched",
        website=website,
        last_attempt=None,
    )


def _criteria(**values: object) -> QualificationCriteria:
    base: dict[str, object] = {"target_keywords": ["saas"]}
    base.update(values)
    return QualificationCriteria.model_validate(base)


def _factor(evaluation, key: str):  # type: ignore[no-untyped-def]
    return next(f for f in evaluation.factors if f.key == key)


# -- criteria validation ------------------------------------------------------
def test_default_criteria_are_documented_values() -> None:
    criteria = QualifyRequest().criteria
    assert criteria.profile == DEFAULT_PROFILE
    assert criteria.target_keywords == sorted(DEFAULT_TARGET_KEYWORDS)
    assert criteria.target_locations == []
    assert criteria.target_tlds == []
    assert criteria.excluded_keywords == []
    assert criteria.threshold == DEFAULT_THRESHOLD == 60
    assert criteria.min_evidence_coverage == pytest.approx(DEFAULT_MIN_EVIDENCE_COVERAGE)
    assert DEFAULT_MIN_EVIDENCE_COVERAGE == pytest.approx(0.5)


def test_terms_are_normalized_deduplicated_and_sorted() -> None:
    criteria = _criteria(
        target_keywords=["  SaaS ", "saas", "B2B", "E-Commerce  Platform"],
        target_locations=["Berlin", "BERLIN"],
        target_tlds=[".DE", "de", "io"],
    )
    assert criteria.target_keywords == ["b2b", "e-commerce platform", "saas"]
    assert criteria.target_locations == ["berlin"]
    assert criteria.target_tlds == ["de", "io"]


@pytest.mark.parametrize(
    "values",
    [
        {"target_keywords": []},
        {"target_keywords": [""]},
        {"target_keywords": ["   "]},
        {"target_keywords": ["a"]},
        {"target_keywords": ["x" * 61]},
        {"target_keywords": ["drop table;"]},
        {"target_keywords": ["<script>"]},
        {"target_keywords": [123]},
        {"target_keywords": "saas"},
        {"target_keywords": [f"term{i}" for i in range(MAX_TERMS + 1)]},
        {"excluded_keywords": ["%"]},
        {"target_tlds": ["d"]},
        {"target_tlds": ["co.uk"]},
        {"target_tlds": ["1de"]},
        {"threshold": 0},
        {"threshold": 101},
        {"min_evidence_coverage": 0},
        {"min_evidence_coverage": 1.5},
        {"profile": ""},
        {"profile": "bad/profile"},
        {"weights": {"industry_fit": 100}},
    ],
)
def test_invalid_criteria_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _criteria(**values)


def test_qualify_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        QualifyRequest.model_validate({"criteria": {}, "url": "http://x"})


# -- industry factor ----------------------------------------------------------
def test_industry_matched_from_discovery_snippet() -> None:
    evaluation = evaluate(_company(description="Acme is a SaaS vendor."), _criteria())
    factor = _factor(evaluation, "industry_fit")
    assert factor.result == "matched"
    assert factor.points == INDUSTRY_WEIGHT
    assert factor.signals_found == ["saas"]
    assert factor.evidence_level == "discovery"
    (evidence,) = factor.evidence
    assert evidence.source == "candidate.description"
    assert evidence.level == "discovery"
    assert evidence.counted is True
    assert evidence.source_url == "https://acme.de/"
    assert evidence.research_id is None
    assert "SaaS" in evidence.excerpt
    # A search-result match alone is preliminary, never a saveable decision.
    assert evaluation.status == "preliminary"


def test_industry_matched_from_research_with_provenance() -> None:
    evaluation = evaluate(_company(website=_research()), _criteria(target_keywords=["b2b"]))
    (evidence,) = _factor(evaluation, "industry_fit").evidence
    assert evidence.source == "research.extracted_text"
    assert evidence.source_url == "https://acme.de/en"
    assert evidence.research_id == 11
    assert evaluation.research_id == 11


def test_matching_is_case_insensitive_and_whole_word() -> None:
    company = _company(website=_research("Our saasy SAASification tools."))
    assert _factor(evaluate(company, _criteria()), "industry_fit").result == "not_matched"
    company = _company(website=_research("Built for (SaaS) teams."))
    assert _factor(evaluate(company, _criteria()), "industry_fit").result == "matched"


def test_multi_word_terms_match_across_whitespace() -> None:
    company = _company(website=_research("Leading supply\n  chain   software."))
    factor = _factor(evaluate(company, _criteria(target_keywords=["supply chain"])), "industry_fit")
    assert factor.result == "matched"


def test_website_evidence_is_preferred_over_discovery_data() -> None:
    company = _company(
        name="SaaS Corp",
        description="A SaaS company.",
        website=_research("More SaaS."),
    )
    factor = _factor(evaluate(company, _criteria()), "industry_fit")
    (evidence,) = factor.evidence
    assert evidence.source == "research.extracted_text"
    assert evidence.level == "website"
    assert factor.evidence_level == "website"


def test_discovery_source_order_is_name_then_description() -> None:
    company = _company(name="SaaS Corp", description="A SaaS company.")
    (evidence,) = _factor(evaluate(company, _criteria()), "industry_fit").evidence
    assert evidence.source == "candidate.name"


def test_industry_absence_with_complete_research_is_not_matched() -> None:
    company = _company(website=_research("We make industrial furniture."))
    evaluation = evaluate(company, _criteria())
    factor = _factor(evaluation, "industry_fit")
    assert factor.result == "not_matched"
    assert factor.points == 0
    assert factor.signals_not_found == ["saas"]
    assert evaluation.status == "unqualified"
    assert evaluation.score == 0
    assert evaluation.evidence_coverage == pytest.approx(1.0)


def test_industry_absence_without_research_is_unknown() -> None:
    evaluation = evaluate(_company(description="A company."), _criteria())
    factor = _factor(evaluation, "industry_fit")
    assert factor.result == "unknown"
    assert "no usable website research" in factor.explanation
    assert evaluation.status == "insufficient_evidence"
    assert evaluation.score == 0
    assert evaluation.evidence_coverage == pytest.approx(0.0)


def test_industry_absence_with_partial_research_is_unknown() -> None:
    company = _company(website=_research("Industrial furniture.", outcome="partial"))
    factor = _factor(evaluate(company, _criteria()), "industry_fit")
    assert factor.result == "unknown"
    assert "partial" in factor.explanation


def test_partial_research_still_supports_positive_matches() -> None:
    company = _company(website=_research("A SaaS platform.", outcome="partial"))
    assert _factor(evaluate(company, _criteria()), "industry_fit").result == "matched"


# -- missing website / research ------------------------------------------------
def test_missing_website_is_not_treated_as_a_bad_prospect() -> None:
    company = _company(
        website_url=None, domain=None, description="Berlin-based B2B SaaS startup."
    )
    evaluation = evaluate(company, _criteria())
    # Neither unqualified nor insufficient: a promising lead awaiting research.
    assert evaluation.status == "preliminary"
    assert evaluation.score == 100
    assert evaluation.research_id is None


def test_no_evidence_at_all_is_insufficient_not_unqualified() -> None:
    company = _company(website_url=None, domain=None, description=None, name="Acme")
    evaluation = evaluate(
        company, _criteria(target_locations=["berlin"], excluded_keywords=["casino"])
    )
    assert evaluation.status == "insufficient_evidence"
    assert {f.result for f in evaluation.factors} == {"unknown"}


# -- location factor ------------------------------------------------------------
def test_location_not_applicable_without_criteria() -> None:
    factor = _factor(evaluate(_company(), _criteria()), "location_fit")
    assert factor.result == "not_applicable"
    assert factor.points == 0


def test_location_matched_from_text() -> None:
    company = _company(website=_research())
    criteria = _criteria(target_locations=["berlin", "munich"])
    factor = _factor(evaluate(company, criteria), "location_fit")
    assert factor.result == "matched"
    assert factor.points == LOCATION_WEIGHT
    assert factor.signals_found == ["berlin"]
    assert factor.signals_not_found == ["munich"]


def test_location_matched_from_domain_tld() -> None:
    evaluation = evaluate(_company(description="SaaS"), _criteria(target_tlds=["de"]))
    factor = _factor(evaluation, "location_fit")
    assert factor.result == "matched"
    assert factor.signals_found == [".de"]
    assert factor.evidence[0].source == "candidate.domain"


def test_tld_requires_label_boundary() -> None:
    company = _company(domain="acmede.com", description="SaaS")
    factor = _factor(evaluate(company, _criteria(target_tlds=["de"])), "location_fit")
    assert factor.result == "unknown"


def test_location_absence_with_complete_research_is_not_matched() -> None:
    company = _company(domain="acme.com", website=_research("A SaaS tool from Lisbon."))
    factor = _factor(evaluate(company, _criteria(target_locations=["berlin"])), "location_fit")
    assert factor.result == "not_matched"


# -- exclusion factor -------------------------------------------------------------
def test_excluded_term_disqualifies_even_with_high_score() -> None:
    company = _company(website=_research("B2B SaaS. We are also a recruitment agency."))
    evaluation = evaluate(
        company,
        _criteria(target_keywords=["saas", "b2b"], excluded_keywords=["recruitment agency"]),
    )
    factor = _factor(evaluation, "exclusion_check")
    assert factor.result == "not_matched"
    assert factor.disqualifying is True
    assert factor.signals_found == ["recruitment agency"]
    assert evaluation.status == "unqualified"
    assert "excluded term" in evaluation.rationale


def test_excluded_term_in_discovery_snippet_disqualifies_without_research() -> None:
    evaluation = evaluate(
        _company(description="Online casino and SaaS."),
        _criteria(excluded_keywords=["casino"]),
    )
    assert evaluation.status == "unqualified"


def test_no_excluded_terms_with_complete_research_is_matched() -> None:
    company = _company(website=_research())
    factor = _factor(evaluate(company, _criteria(excluded_keywords=["casino"])), "exclusion_check")
    assert factor.result == "matched"
    assert factor.points == EXCLUSION_WEIGHT


def test_no_excluded_terms_without_complete_research_is_unknown() -> None:
    company = _company(description="SaaS", website=_research("SaaS", outcome="partial"))
    factor = _factor(evaluate(company, _criteria(excluded_keywords=["casino"])), "exclusion_check")
    assert factor.result == "unknown"
    assert factor.disqualifying is False


def test_exclusion_not_applicable_without_criteria() -> None:
    assert _factor(evaluate(_company(), _criteria()), "exclusion_check").result == "not_applicable"


# -- score, threshold, and coverage boundaries -------------------------------------
def _industry_hit_location_miss() -> EnrichedCompany:
    # industry matched (50) + location not_matched (30): 50/80 = 62.5 -> 63.
    return _company(domain="acme.com", website=_research("A SaaS tool from Lisbon."))


def test_score_rounds_half_up() -> None:
    evaluation = evaluate(_industry_hit_location_miss(), _criteria(target_locations=["berlin"]))
    assert evaluation.score == 63
    assert evaluation.evidence_coverage == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("threshold", "status"), [(62, "qualified"), (63, "qualified"), (64, "unqualified")]
)
def test_threshold_boundary(threshold: int, status: str) -> None:
    evaluation = evaluate(
        _industry_hit_location_miss(),
        _criteria(target_locations=["berlin"], threshold=threshold),
    )
    assert evaluation.status == status
    assert evaluation.threshold == threshold


@pytest.mark.parametrize(
    ("text", "keywords", "locations", "excluded", "expected"),
    [
        ("SaaS in Berlin.", ["saas"], ["berlin"], ["casino"], 100),
        ("Furniture in Berlin.", ["saas"], ["berlin"], ["casino"], 50),  # 0+30+20 of 100
        ("SaaS in Lisbon.", ["saas"], ["berlin"], ["casino"], 70),  # 50+0+20 of 100
        ("Furniture in Lisbon.", ["saas"], ["berlin"], ["casino"], 20),
        ("Furniture in Lisbon.", ["saas"], ["berlin"], [], 0),
    ],
)
def test_score_combinations(
    text: str, keywords: list[str], locations: list[str], excluded: list[str], expected: int
) -> None:
    company = _company(domain="acme.com", website=_research(text, title=None))
    evaluation = evaluate(
        company,
        _criteria(target_keywords=keywords, target_locations=locations, excluded_keywords=excluded),
    )
    assert evaluation.score == expected


def _partial_saas_page() -> EnrichedCompany:
    # Industry is confirmed on a partial page; location and exclusion stay
    # unknown because a partial page cannot prove absence.
    return _company(domain="acme.com", website=_research("SaaS company.", outcome="partial"))


def test_unknown_factors_do_not_lower_the_score() -> None:
    evaluation = evaluate(
        _partial_saas_page(), _criteria(target_locations=["berlin"], excluded_keywords=["casino"])
    )
    assert evaluation.score == 100
    assert evaluation.evidence_coverage == pytest.approx(0.5)
    assert evaluation.status == "qualified"


@pytest.mark.parametrize(
    ("minimum", "status"), [(0.5, "qualified"), (0.51, "insufficient_evidence")]
)
def test_evidence_coverage_boundary(minimum: float, status: str) -> None:
    company = _partial_saas_page()
    evaluation = evaluate(
        company,
        _criteria(
            target_locations=["berlin"], excluded_keywords=["casino"], min_evidence_coverage=minimum
        ),
    )
    assert evaluation.status == status


def test_score_is_bounded() -> None:
    for company in (_company(), _company(website=_research()), _industry_hit_location_miss()):
        evaluation = evaluate(company, _criteria(target_locations=["berlin"]))
        assert 0 <= evaluation.score <= 100
        assert 0 <= evaluation.evidence_coverage <= 1


# -- determinism and explanation --------------------------------------------------
def test_identical_inputs_produce_identical_results() -> None:
    criteria = _criteria(
        target_keywords=["saas", "b2b"], target_locations=["berlin"], excluded_keywords=["casino"]
    )
    first = evaluate(_company(website=_research()), criteria)
    second = evaluate(_company(website=_research()), criteria)
    assert first == second
    assert first.rule_version == RULE_VERSION == "qualification-rules/v2"


def test_equivalent_criteria_spellings_give_identical_results() -> None:
    company = _company(website=_research())
    a = evaluate(company, _criteria(target_keywords=["SaaS", "b2b"]))
    b = evaluate(company, _criteria(target_keywords=["b2b", "saas", "SAAS"]))
    assert a == b


def test_rationale_explains_every_factor() -> None:
    evaluation = evaluate(
        _company(website=_research()),
        _criteria(target_locations=["berlin"], excluded_keywords=["casino"]),
    )
    lines = evaluation.rationale.splitlines()
    assert lines[0].startswith("Qualified: score 100 meets the threshold 60.")
    assert RULE_VERSION in lines[0]
    assert len(lines) == 4
    assert lines[1].startswith("- Industry fit (matched, website evidence, 50/50)")
    assert lines[2].startswith("- Location fit (matched, website evidence, 30/30)")
    assert lines[3].startswith("- Exclusion check (matched, website evidence, 20/20)")


def test_excerpts_are_bounded() -> None:
    long_text = ("filler " * 200) + "SaaS" + (" filler" * 200)
    (evidence,) = _factor(
        evaluate(_company(website=_research(long_text)), _criteria()), "industry_fit"
    ).evidence
    assert len(evidence.excerpt) <= 170
    assert evidence.excerpt.startswith("…") and evidence.excerpt.endswith("…")


# -- v2: misleading search results and evidence levels ------------------------------
LISTICLE_TITLE = "Best Software Companies to Work for in Berlin | Wellfound"
LISTICLE_SNIPPET = "Discover the top 50 software and SaaS startups hiring in Berlin."
DEFAULTS = QualificationCriteria()  # b2b, platform, saas, software; threshold 60


def test_misleading_title_without_research_is_only_preliminary() -> None:
    company = _company(name=LISTICLE_TITLE, description=LISTICLE_SNIPPET, domain="wellfound.com")
    evaluation = evaluate(company, DEFAULTS)
    industry = _factor(evaluation, "industry_fit")
    assert industry.result == "matched"
    assert industry.evidence_level == "discovery"
    assert industry.signals_found == ["saas", "software"]
    assert "not yet confirmed by the company's website" in industry.explanation
    assert evaluation.score == 100
    assert evaluation.status == "preliminary"
    assert "research the company's website" in evaluation.rationale


def test_misleading_title_is_refuted_by_complete_research() -> None:
    company = _company(
        name=LISTICLE_TITLE,
        description=LISTICLE_SNIPPET,
        domain="oakworks.com",
        website=_research("Handcrafted oak furniture made in Bavaria.", title="Oakworks"),
    )
    criteria = _criteria(target_keywords=["software", "saas"], target_locations=["berlin"])
    evaluation = evaluate(company, criteria)
    industry = _factor(evaluation, "industry_fit")
    location = _factor(evaluation, "location_fit")
    assert industry.result == "not_matched"
    assert industry.signals_found == []
    assert industry.signals_unconfirmed == ["saas", "software"]
    assert all(item.counted is False for item in industry.evidence)
    assert "not on the complete website page" in industry.explanation
    assert location.result == "not_matched"
    assert location.signals_unconfirmed == ["berlin"]
    assert evaluation.score == 0
    assert evaluation.status == "unqualified"


def test_generic_term_in_snippet_with_partial_research_stays_preliminary() -> None:
    company = _company(
        description="Enterprise software reviews and rankings.",
        website=_research("Furniture catalogue page 1 of 40", outcome="partial", title=None),
    )
    evaluation = evaluate(company, _criteria(target_keywords=["software"]))
    industry = _factor(evaluation, "industry_fit")
    assert industry.result == "matched"
    assert industry.evidence_level == "discovery"
    assert "partial" in industry.explanation
    assert evaluation.status == "preliminary"


def test_website_confirmation_upgrades_preliminary_to_qualified() -> None:
    snippet_only = _company(description="A B2B SaaS platform.")
    confirmed = _company(
        description="A B2B SaaS platform.",
        website=_research("Acme is a B2B SaaS platform for fleets."),
    )
    assert evaluate(snippet_only, DEFAULTS).status == "preliminary"
    evaluation = evaluate(confirmed, DEFAULTS)
    assert evaluation.status == "qualified"
    assert _factor(evaluation, "industry_fit").evidence_level == "website"


def test_partial_research_match_counts_as_website_evidence() -> None:
    company = _company(website=_research("A SaaS platform.", outcome="partial"))
    evaluation = evaluate(company, _criteria())
    assert _factor(evaluation, "industry_fit").evidence_level == "website"
    assert evaluation.status == "qualified"


def test_mixed_website_and_unconfirmed_discovery_terms() -> None:
    company = _company(
        name="Top Software Vendors",
        website=_research("Acme is a B2B SaaS company."),
    )
    industry = _factor(evaluate(company, DEFAULTS), "industry_fit")
    assert industry.signals_found == ["b2b", "saas"]
    assert industry.signals_unconfirmed == ["software"]
    assert industry.signals_not_found == ["platform"]
    assert industry.evidence_level == "website"


def test_discovery_terms_still_count_alongside_website_terms_without_complete_page() -> None:
    company = _company(
        name="Top Software Vendors",
        website=_research("Acme is a SaaS company.", outcome="partial"),
    )
    criteria = _criteria(target_keywords=["saas", "software"])
    industry = _factor(evaluate(company, criteria), "industry_fit")
    assert industry.signals_found == ["saas", "software"]
    assert industry.signals_unconfirmed == []
    assert {e.level for e in industry.evidence} == {"website", "discovery"}
    assert industry.evidence_level == "website"


def test_domain_tld_counts_even_when_complete_page_lacks_location() -> None:
    company = _company(domain="acme.de", website=_research("A SaaS company.", title=None))
    location = _factor(evaluate(company, _criteria(target_tlds=["de"])), "location_fit")
    assert location.result == "matched"
    assert location.evidence_level == "domain"


def test_industry_is_required_even_if_other_factors_reach_threshold() -> None:
    company = _company(domain="acme.com", website=_research("Furniture made in Berlin."))
    evaluation = evaluate(
        company,
        _criteria(target_locations=["berlin"], excluded_keywords=["casino"], threshold=50),
    )
    assert evaluation.score == 50  # 0 + 30 + 20 of 100
    assert evaluation.status == "unqualified"
    assert "industry fit, a required factor, is not met" in evaluation.rationale


def test_unknown_industry_with_low_coverage_minimum_is_insufficient() -> None:
    company = _company(
        domain="acme.com",
        website=_research("Offices in Berlin.", outcome="partial", title=None),
    )
    evaluation = evaluate(
        company,
        _criteria(
            target_locations=["berlin"], excluded_keywords=["casino"], min_evidence_coverage=0.3
        ),
    )
    assert _factor(evaluation, "industry_fit").result == "unknown"
    assert evaluation.evidence_coverage == pytest.approx(0.3)
    assert evaluation.score == 100
    assert evaluation.status == "insufficient_evidence"
    assert "could not be evaluated" in evaluation.rationale


@pytest.mark.parametrize(
    ("threshold", "status"), [(63, "qualified"), (64, "unqualified")]
)
def test_threshold_boundary_with_website_evidence(threshold: int, status: str) -> None:
    evaluation = evaluate(
        _industry_hit_location_miss(), _criteria(target_locations=["berlin"], threshold=threshold)
    )
    assert evaluation.score == 63
    assert evaluation.status == status


def test_preliminary_results_always_score_100() -> None:
    """Discovery-only matches count only without a complete page, and only a
    complete page can produce ``not_matched``; so nothing can lower the score
    of a preliminary result, even at the highest threshold."""
    company = _company(
        domain="acme.com",
        description="SaaS",
        website=_research("Catalogue page", outcome="partial", title=None),
    )
    evaluation = evaluate(company, _criteria(threshold=100, target_tlds=["de"]))
    assert (evaluation.score, evaluation.status) == (100, "preliminary")
    assert _factor(evaluation, "location_fit").result == "unknown"


def test_excluded_term_in_listicle_title_still_disqualifies_conservatively() -> None:
    company = _company(
        name="Top 10 Recruitment Agency Picks",
        website=_research("Acme is a B2B SaaS company."),
    )
    evaluation = evaluate(
        company, _criteria(target_keywords=["saas"], excluded_keywords=["recruitment agency"])
    )
    exclusion = _factor(evaluation, "exclusion_check")
    assert exclusion.disqualifying is True
    assert exclusion.evidence[0].level == "discovery"
    assert evaluation.status == "unqualified"


def test_non_matched_factors_have_no_evidence_level() -> None:
    evaluation = evaluate(_company(website=_research("Furniture.")), _criteria())
    assert _factor(evaluation, "industry_fit").evidence_level is None
    assert _factor(evaluation, "location_fit").evidence_level is None


def test_v2_results_are_deterministic_for_misleading_inputs() -> None:
    company = _company(name=LISTICLE_TITLE, description=LISTICLE_SNIPPET)
    assert evaluate(company, DEFAULTS) == evaluate(company, DEFAULTS)


# -- criteria and stored-row compatibility ------------------------------------------
@pytest.mark.parametrize(
    "values",
    [
        {"target_keywords": ["saas"], "excluded_keywords": ["SaaS"]},
        {
            "target_keywords": ["saas"],
            "target_locations": ["berlin"],
            "excluded_keywords": ["berlin"],
        },
    ],
)
def test_contradictory_criteria_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="must not repeat target terms"):
        QualificationCriteria.model_validate(values)


def test_v1_factor_payload_still_parses() -> None:
    from app.schemas.qualification import FactorOutcome

    v1 = {
        "key": "industry_fit",
        "label": "Industry fit",
        "weight": 50,
        "result": "matched",
        "points": 50,
        "disqualifying": False,
        "signals_found": ["saas"],
        "signals_not_found": [],
        "evidence": [
            {
                "signal": "saas",
                "source": "candidate.name",
                "source_url": "https://acme.de/",
                "research_id": None,
                "excerpt": "SaaS Corp",
            }
        ],
        "explanation": "Found 'saas' in candidate.name.",
    }
    factor = FactorOutcome.model_validate(v1)
    assert factor.signals_unconfirmed == []
    assert factor.evidence_level is None
    assert factor.evidence[0].level is None


# -- de-duplication of signals and evidence ----------------------------------------
def _signal_lists(factor) -> list[str]:  # type: ignore[no-untyped-def]
    return factor.signals_found + factor.signals_unconfirmed + factor.signals_not_found


def test_term_in_every_source_is_reported_once_with_website_provenance() -> None:
    company = _company(
        name="SaaS Corp",
        description="A SaaS company.",
        website=_research("We sell SaaS. More SaaS. Even more SaaS.", title="SaaS for teams"),
    )
    industry = _factor(evaluate(company, _criteria()), "industry_fit")
    assert industry.signals_found == ["saas"]
    assert industry.signals_unconfirmed == []
    assert industry.signals_not_found == []
    (evidence,) = industry.evidence
    # The first website source in the fixed order wins.
    assert (evidence.source, evidence.level, evidence.research_id) == (
        "research.page_title",
        "website",
        11,
    )


def test_term_in_several_discovery_sources_is_reported_once() -> None:
    company = _company(name="SaaS Directory", description="Top SaaS picks; SaaS SaaS.")
    industry = _factor(evaluate(company, _criteria()), "industry_fit")
    assert industry.signals_found == ["saas"]
    (evidence,) = industry.evidence
    assert evidence.source == "candidate.name"


def test_unconfirmed_term_in_several_sources_is_reported_once() -> None:
    company = _company(
        name="Best Software Companies",
        description="Software, software, and more software.",
        website=_research("Oak furniture.", title="Oakworks"),
    )
    industry = _factor(evaluate(company, _criteria(target_keywords=["software"])), "industry_fit")
    assert industry.signals_unconfirmed == ["software"]
    assert industry.signals_found == []
    assert industry.signals_not_found == []
    (evidence,) = industry.evidence
    assert (evidence.source, evidence.counted) == ("candidate.name", False)


def test_excluded_term_in_several_sources_is_reported_once() -> None:
    company = _company(
        name="Casino Reviews",
        description="Online casino guide.",
        website=_research("Best casino bonuses. SaaS casino tools."),
    )
    exclusion = _factor(
        evaluate(company, _criteria(excluded_keywords=["casino"])), "exclusion_check"
    )
    assert exclusion.signals_found == ["casino"]
    (evidence,) = exclusion.evidence
    assert evidence.source == "research.extracted_text"


def test_every_target_lands_in_exactly_one_list_in_criteria_order() -> None:
    company = _company(
        name="Top Software Vendors",
        domain="acme.de",
        website=_research("Acme is a B2B SaaS company in Munich."),
    )
    criteria = _criteria(
        target_keywords=["b2b", "platform", "saas", "software"],
        target_locations=["berlin", "munich"],
        target_tlds=["de", "io"],
    )
    evaluation = evaluate(company, criteria)
    industry = _factor(evaluation, "industry_fit")
    location = _factor(evaluation, "location_fit")
    assert industry.signals_found == ["b2b", "saas"]
    assert industry.signals_unconfirmed == ["software"]
    assert industry.signals_not_found == ["platform"]
    assert location.signals_found == ["munich", ".de"]
    assert location.signals_not_found == ["berlin", ".io"]
    for factor in (industry, location):
        signals = _signal_lists(factor)
        assert len(signals) == len(set(signals))


def test_non_canonical_criteria_cannot_duplicate_signals() -> None:
    # Bypasses validation on purpose: evaluate() must still report each term once.
    criteria = QualificationCriteria.model_construct(
        profile="raw",
        target_keywords=["saas", "saas", "b2b", "b2b"],
        target_locations=["berlin", "berlin"],
        target_tlds=["de", "de"],
        excluded_keywords=["casino", "casino"],
        threshold=60,
        min_evidence_coverage=0.5,
    )
    company = _company(domain="acme.de", website=_research("B2B SaaS in Berlin."))
    evaluation = evaluate(company, criteria)
    for factor in evaluation.factors:
        signals = _signal_lists(factor)
        assert len(signals) == len(set(signals)), factor.key
        assert len({e.signal for e in factor.evidence}) == len(factor.evidence)
    assert _factor(evaluation, "industry_fit").signals_found == ["saas", "b2b"]
    assert _factor(evaluation, "location_fit").signals_found == ["berlin", ".de"]


def test_deduplication_is_deterministic() -> None:
    company = _company(
        name="SaaS SaaS", description="SaaS", website=_research("SaaS", title="SaaS")
    )
    first = evaluate(company, DEFAULTS)
    second = evaluate(company, DEFAULTS)
    assert first.factors == second.factors
    assert first.rationale == second.rationale
