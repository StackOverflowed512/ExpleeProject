"""Deterministic, versioned qualification rules.

This module is pure: no database, network, clock, or randomness. The same
:class:`EnrichedCompany` and :class:`QualificationCriteria` always produce
the same :class:`Evaluation` for a given :data:`RULE_VERSION`. Any change
to factors, weights, matching, or status logic must bump the version.

Rules (``qualification-rules/v1``)
---------------------------------
Evidence comes only from sourced fields, checked in this fixed order:
``candidate.name``, ``candidate.description`` (discovery snippet),
``research.page_title``, ``research.extracted_text`` (latest usable
research), and ``candidate.domain`` for TLD checks. Matching is
case-insensitive and whole-word.

* ``industry_fit`` (weight 50): *matched* when a target keyword is found;
  *not_matched* when none is found and the research page is complete;
  otherwise *unknown*.
* ``location_fit`` (weight 30): *matched* when a target location is found
  or the domain ends in a target TLD; *not_matched* when none is found and
  the research page is complete; otherwise *unknown*.
* ``exclusion_check`` (weight 20): *not_matched* and **disqualifying** when
  an excluded term is found; *matched* when none is found and the research
  page is complete; otherwise *unknown*.

A factor without criteria (no locations/TLDs, no excluded keywords) is
``not_applicable`` and ignored. Absence of a term is only treated as
evidence when a *complete* (``success``) website page was read; a short
discovery snippet or a truncated page never proves absence.

* ``score`` = round-half-up(100 × points ÷ evaluated weight), where evaluated
  factors are ``matched`` or ``not_matched``; 0 when nothing was evaluated.
  Unknown factors therefore never lower the score.
* ``evidence_coverage`` = evaluated weight ÷ applicable weight.
* ``status``: ``unqualified`` if any factor is disqualifying; otherwise
  ``insufficient_evidence`` if coverage < ``min_evidence_coverage``;
  otherwise ``qualified`` if score ≥ ``threshold``, else ``unqualified``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.qualification import (
    Evidence,
    EvidenceSource,
    FactorKey,
    FactorOutcome,
    FactorResult,
    QualificationCriteria,
    QualificationStatus,
)
from app.schemas.research import EnrichedCompany

RULE_VERSION = "qualification-rules/v1"
INDUSTRY_WEIGHT = 50
LOCATION_WEIGHT = 30
EXCLUSION_WEIGHT = 20
EXCERPT_RADIUS = 70


@dataclass(frozen=True)
class _Source:
    name: EvidenceSource
    text: str
    url: str | None
    research_id: int | None


@dataclass(frozen=True)
class Evaluation:
    """Result of applying the rules; persisted by the qualification service."""

    rule_version: str
    status: QualificationStatus
    score: int
    threshold: int
    evidence_coverage: float
    factors: list[FactorOutcome]
    rationale: str
    research_id: int | None


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _term_pattern(term: str) -> re.Pattern[str]:
    words = [re.escape(word) for word in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - EXCERPT_RADIUS)
    right = min(len(text), end + EXCERPT_RADIUS)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{text[left:right].strip()}{suffix}"


def _sources(company: EnrichedCompany) -> list[_Source]:
    discovery_url = company.discovery.source_url
    sources = [_Source("candidate.name", _clean(company.name), discovery_url, None)]
    if company.description:
        sources.append(
            _Source("candidate.description", _clean(company.description), discovery_url, None)
        )
    website = company.website
    if website is not None:
        url = website.final_url or website.requested_url
        if website.page_title:
            sources.append(
                _Source("research.page_title", _clean(website.page_title), url, website.id)
            )
        if website.extracted_text:
            sources.append(
                _Source("research.extracted_text", _clean(website.extracted_text), url, website.id)
            )
    return sources


def _find(term: str, sources: list[_Source]) -> Evidence | None:
    pattern = _term_pattern(term)
    for source in sources:
        match = pattern.search(source.text)
        if match is not None:
            return Evidence(
                signal=term,
                source=source.name,
                source_url=source.url,
                research_id=source.research_id,
                excerpt=_excerpt(source.text, match.start(), match.end()),
            )
    return None


def _research_note(company: EnrichedCompany) -> str:
    if company.website is None:
        return "no usable website research is stored"
    return "the stored website research is partial (a size limit was hit)"


def _complete_research(company: EnrichedCompany) -> bool:
    website = company.website
    return website is not None and website.outcome == "success" and bool(website.extracted_text)


def _quote(terms: list[str]) -> str:
    return ", ".join(f"'{term}'" for term in terms)


def _positive_factor(
    *,
    key: FactorKey,
    label: str,
    weight: int,
    targets: list[str],
    evidence: list[Evidence],
    complete: bool,
    research_note: str,
) -> FactorOutcome:
    found = [item.signal for item in evidence]
    not_found = [term for term in targets if term not in found]
    result: FactorResult
    if found:
        result, points = "matched", weight
        explanation = f"Found {_quote(found)} in " + ", ".join(
            sorted({item.source for item in evidence})
        ) + "."
    elif complete:
        result, points = "not_matched", 0
        explanation = (
            f"None of {_quote(targets)} appear in the discovery data or the complete "
            "website page."
        )
    else:
        result, points = "unknown", 0
        explanation = (
            f"None of {_quote(targets)} appear in the available data, but {research_note}, "
            "so their absence is not treated as evidence."
        )
    return FactorOutcome(
        key=key,
        label=label,
        weight=weight,
        result=result,
        points=points,
        disqualifying=False,
        signals_found=found,
        signals_not_found=not_found,
        evidence=evidence,
        explanation=explanation,
    )


def _not_applicable(key: FactorKey, label: str, weight: int, reason: str) -> FactorOutcome:
    return FactorOutcome(
        key=key,
        label=label,
        weight=weight,
        result="not_applicable",
        points=0,
        disqualifying=False,
        signals_found=[],
        signals_not_found=[],
        evidence=[],
        explanation=reason,
    )


def _industry(
    company: EnrichedCompany,
    criteria: QualificationCriteria,
    sources: list[_Source],
) -> FactorOutcome:
    evidence = [e for e in (_find(t, sources) for t in criteria.target_keywords) if e]
    return _positive_factor(
        key="industry_fit",
        label="Industry fit",
        weight=INDUSTRY_WEIGHT,
        targets=criteria.target_keywords,
        evidence=evidence,
        complete=_complete_research(company),
        research_note=_research_note(company),
    )


def _location(
    company: EnrichedCompany,
    criteria: QualificationCriteria,
    sources: list[_Source],
) -> FactorOutcome:
    if not criteria.target_locations and not criteria.target_tlds:
        return _not_applicable(
            "location_fit",
            "Location fit",
            LOCATION_WEIGHT,
            "No target locations or TLDs are configured.",
        )
    evidence = [e for e in (_find(t, sources) for t in criteria.target_locations) if e]
    domain = (company.domain or "").casefold()
    for tld in criteria.target_tlds:
        if domain.endswith(f".{tld}"):
            evidence.append(
                Evidence(
                    signal=f".{tld}",
                    source="candidate.domain",
                    source_url=company.discovery.source_url,
                    excerpt=domain,
                )
            )
    return _positive_factor(
        key="location_fit",
        label="Location fit",
        weight=LOCATION_WEIGHT,
        targets=criteria.target_locations + [f".{tld}" for tld in criteria.target_tlds],
        evidence=evidence,
        complete=_complete_research(company),
        research_note=_research_note(company),
    )


def _exclusion(
    company: EnrichedCompany,
    criteria: QualificationCriteria,
    sources: list[_Source],
) -> FactorOutcome:
    label = "Exclusion check"
    if not criteria.excluded_keywords:
        return _not_applicable(
            "exclusion_check", label, EXCLUSION_WEIGHT, "No excluded keywords are configured."
        )
    evidence = [e for e in (_find(t, sources) for t in criteria.excluded_keywords) if e]
    found = [item.signal for item in evidence]
    not_found = [term for term in criteria.excluded_keywords if term not in found]
    if found:
        return FactorOutcome(
            key="exclusion_check",
            label=label,
            weight=EXCLUSION_WEIGHT,
            result="not_matched",
            points=0,
            disqualifying=True,
            signals_found=found,
            signals_not_found=not_found,
            evidence=evidence,
            explanation=(
                f"Excluded term(s) {_quote(found)} found in "
                + ", ".join(sorted({item.source for item in evidence}))
                + "; this disqualifies the candidate."
            ),
        )
    if _complete_research(company):
        return FactorOutcome(
            key="exclusion_check",
            label=label,
            weight=EXCLUSION_WEIGHT,
            result="matched",
            points=EXCLUSION_WEIGHT,
            disqualifying=False,
            signals_found=[],
            signals_not_found=not_found,
            evidence=[],
            explanation=(
                f"None of {_quote(not_found)} appear in the discovery data or the "
                "complete website page."
            ),
        )
    return FactorOutcome(
        key="exclusion_check",
        label=label,
        weight=EXCLUSION_WEIGHT,
        result="unknown",
        points=0,
        disqualifying=False,
        signals_found=[],
        signals_not_found=not_found,
        evidence=[],
        explanation=(
            f"No excluded terms appear in the available data, but {_research_note(company)}, "
            "so their absence cannot be confirmed."
        ),
    )


def _score(points: int, evaluated_weight: int) -> int:
    if evaluated_weight == 0:
        return 0
    # Integer round-half-up keeps the result exact and platform independent.
    return (200 * points + evaluated_weight) // (2 * evaluated_weight)


def evaluate(company: EnrichedCompany, criteria: QualificationCriteria) -> Evaluation:
    """Apply :data:`RULE_VERSION` rules to one enriched company."""
    sources = _sources(company)
    factors = [
        _industry(company, criteria, sources),
        _location(company, criteria, sources),
        _exclusion(company, criteria, sources),
    ]

    applicable = [f for f in factors if f.result != "not_applicable"]
    evaluated = [f for f in applicable if f.result in ("matched", "not_matched")]
    applicable_weight = sum(f.weight for f in applicable)
    evaluated_weight = sum(f.weight for f in evaluated)
    points = sum(f.points for f in evaluated)
    score = _score(points, evaluated_weight)
    coverage = round(evaluated_weight / applicable_weight, 4) if applicable_weight else 0.0

    if any(f.disqualifying for f in factors):
        status: QualificationStatus = "unqualified"
        decision = "an excluded term was found"
    elif coverage < criteria.min_evidence_coverage:
        status = "insufficient_evidence"
        decision = (
            f"only {coverage:.0%} of the applicable factor weight could be evaluated "
            f"(minimum {criteria.min_evidence_coverage:.0%})"
        )
    elif score >= criteria.threshold:
        status = "qualified"
        decision = f"score {score} meets the threshold {criteria.threshold}"
    else:
        status = "unqualified"
        decision = f"score {score} is below the threshold {criteria.threshold}"

    lines = [
        f"{status.replace('_', ' ').capitalize()}: {decision}. Score {score}/100 from "
        f"{points} of {evaluated_weight} evaluated points; evidence coverage "
        f"{coverage:.0%}; profile '{criteria.profile}'; rules {RULE_VERSION}.",
    ]
    lines.extend(
        f"- {f.label} ({f.result.replace('_', ' ')}, {f.points}/{f.weight}): {f.explanation}"
        for f in factors
    )

    return Evaluation(
        rule_version=RULE_VERSION,
        status=status,
        score=score,
        threshold=criteria.threshold,
        evidence_coverage=coverage,
        factors=factors,
        rationale="\n".join(lines),
        research_id=company.website.id if company.website is not None else None,
    )
