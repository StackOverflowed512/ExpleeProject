"""Deterministic, versioned qualification rules.

This module is pure: no database, network, clock, or randomness. The same
:class:`EnrichedCompany` and :class:`QualificationCriteria` always produce
the same :class:`Evaluation` for a given :data:`RULE_VERSION`. Any change
to factors, weights, matching, evidence handling, or status logic must bump
the version.

Rules (``qualification-rules/v2``)
---------------------------------
Evidence has a level:

* ``website``: the company's own pages (``research.page_title`` and
  ``research.extracted_text`` of the latest usable research, complete or
  partial);
* ``domain``: the candidate's domain (TLD checks only);
* ``discovery``: the third-party search result (``candidate.name`` is the
  result title, ``candidate.description`` its snippet). Search titles are
  often listicles or directories ("Best SaaS companies in Berlin"), so this
  level is treated as preliminary.

Each term is looked up on the website first, then in discovery data.
Matching is case-insensitive and whole-word.

* ``industry_fit`` (weight 50) and ``location_fit`` (weight 30):
  *matched* when a term is found on the website (or a target TLD matches
  for location). A term found only in discovery data counts as *matched*
  at the ``discovery`` level while research is missing or partial; once a
  *complete* page was read, such terms are reported as unconfirmed and do
  not count. *not_matched* when nothing counts and the research page is
  complete; otherwise *unknown*.
* ``exclusion_check`` (weight 20): *not_matched* and **disqualifying** when
  an excluded term is found anywhere (website or discovery data;
  conservative by design); *matched* when none is found and the research
  page is complete; otherwise *unknown*.

A factor without criteria is ``not_applicable`` and ignored.

* ``score`` = round-half-up(100 × points ÷ evaluated weight), where
  evaluated factors are ``matched`` or ``not_matched``; 0 when nothing was
  evaluated. Unknown factors never lower the score.
* ``evidence_coverage`` = evaluated weight ÷ applicable weight.
* ``status``, in order:

  1. ``unqualified`` if any factor is disqualifying;
  2. ``insufficient_evidence`` if coverage < ``min_evidence_coverage``;
  3. ``unqualified`` if score < ``threshold``;
  4. ``unqualified`` if industry fit is ``not_matched`` (required factor);
  5. ``insufficient_evidence`` if industry fit is ``unknown``;
  6. ``preliminary`` if industry fit is matched only at ``discovery`` level;
  7. otherwise ``qualified``.

Changes from v1: evidence levels; website-first lookup; discovery-only
matches no longer count once a complete page was read; industry fit is a
required factor; and the ``preliminary`` status replaces ``qualified`` when
industry fit rests only on search-result data.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.qualification import (
    Evidence,
    EvidenceLevel,
    EvidenceSource,
    FactorKey,
    FactorOutcome,
    FactorResult,
    QualificationCriteria,
    QualificationStatus,
)
from app.schemas.research import EnrichedCompany

RULE_VERSION = "qualification-rules/v2"
INDUSTRY_WEIGHT = 50
LOCATION_WEIGHT = 30
EXCLUSION_WEIGHT = 20
EXCERPT_RADIUS = 70
_LEVEL_RANK: dict[EvidenceLevel, int] = {"website": 3, "domain": 2, "discovery": 1}


@dataclass(frozen=True)
class _Source:
    name: EvidenceSource
    level: EvidenceLevel
    text: str
    url: str | None
    research_id: int | None


@dataclass(frozen=True)
class _Context:
    """Evidence sources and research state shared by all factors."""

    website: list[_Source]
    discovery: list[_Source]
    complete_research: bool
    research_note: str
    domain: str
    discovery_url: str


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


def _context(company: EnrichedCompany) -> _Context:
    discovery_url = company.discovery.source_url
    discovery = [_Source("candidate.name", "discovery", _clean(company.name), discovery_url, None)]
    if company.description:
        discovery.append(
            _Source(
                "candidate.description",
                "discovery",
                _clean(company.description),
                discovery_url,
                None,
            )
        )
    website_sources: list[_Source] = []
    website = company.website
    if website is not None:
        url = website.final_url or website.requested_url
        if website.page_title:
            website_sources.append(
                _Source(
                    "research.page_title",
                    "website",
                    _clean(website.page_title),
                    url,
                    website.id,
                )
            )
        if website.extracted_text:
            website_sources.append(
                _Source(
                    "research.extracted_text",
                    "website",
                    _clean(website.extracted_text),
                    url,
                    website.id,
                )
            )
    complete = website is not None and website.outcome == "success" and bool(website.extracted_text)
    if website is None:
        note = "no usable website research is stored"
    else:
        note = "the stored website research is partial (a size limit was hit)"
    return _Context(
        website=website_sources,
        discovery=discovery,
        complete_research=complete,
        research_note=note,
        domain=(company.domain or "").casefold(),
        discovery_url=discovery_url,
    )


def _find(term: str, sources: list[_Source], *, counted: bool = True) -> Evidence | None:
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
                level=source.level,
                counted=counted,
            )
    return None


def _quote(terms: list[str]) -> str:
    return ", ".join(f"'{term}'" for term in terms)


def _sources_of(evidence: list[Evidence]) -> str:
    return ", ".join(sorted({item.source for item in evidence}))


def _strongest(evidence: list[Evidence]) -> EvidenceLevel | None:
    levels = [item.level for item in evidence if item.counted and item.level is not None]
    return max(levels, key=lambda level: _LEVEL_RANK[level]) if levels else None


def _positive_factor(
    *,
    key: FactorKey,
    label: str,
    weight: int,
    terms: list[str],
    tlds: list[str],
    ctx: _Context,
) -> FactorOutcome:
    """Evaluate a factor that rewards finding target terms (website first).

    Each distinct term yields at most one evidence item and appears in
    exactly one of ``signals_found``, ``signals_unconfirmed``, or
    ``signals_not_found``, in criteria order, however many sources contain
    it. Validated criteria are already unique; de-duplicating here keeps the
    guarantee for any caller.
    """
    terms = list(dict.fromkeys(terms))
    tlds = list(dict.fromkeys(tlds))
    evidence: list[Evidence] = []
    unconfirmed: list[str] = []
    for term in terms:
        found = _find(term, ctx.website)
        if found is None:
            # Discovery data counts only while the website could not refute it.
            found = _find(term, ctx.discovery, counted=not ctx.complete_research)
            if found is not None and not found.counted:
                unconfirmed.append(term)
        if found is not None:
            evidence.append(found)
    for tld in tlds:
        if ctx.domain.endswith(f".{tld}"):
            evidence.append(
                Evidence(
                    signal=f".{tld}",
                    source="candidate.domain",
                    source_url=ctx.discovery_url,
                    excerpt=ctx.domain,
                    level="domain",
                )
            )

    targets = terms + [f".{tld}" for tld in tlds]
    counted = [item for item in evidence if item.counted]
    found_terms = [item.signal for item in counted]
    not_found = [t for t in targets if t not in found_terms and t not in unconfirmed]
    level = _strongest(counted)
    unconfirmed_note = (
        f" {_quote(unconfirmed)} appear only in search-result data and not on the complete "
        "website page, so they are not counted."
        if unconfirmed
        else ""
    )

    result: FactorResult
    if counted:
        result, points = "matched", weight
        explanation = f"Found {_quote(found_terms)} in {_sources_of(counted)}"
        if level == "discovery":
            explanation += (
                f" (search-result data only; not yet confirmed by the company's website "
                f"because {ctx.research_note})"
            )
        explanation += "." + unconfirmed_note
    elif ctx.complete_research:
        result, points = "not_matched", 0
        explanation = (
            f"None of {_quote(targets)} appear on the complete website page." + unconfirmed_note
        )
    else:
        result, points = "unknown", 0
        explanation = (
            f"None of {_quote(targets)} appear in the available data, but {ctx.research_note}, "
            "so their absence is not treated as evidence."
        )
    return FactorOutcome(
        key=key,
        label=label,
        weight=weight,
        result=result,
        points=points,
        disqualifying=False,
        signals_found=found_terms,
        signals_not_found=not_found,
        signals_unconfirmed=unconfirmed,
        evidence_level=level if result == "matched" else None,
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


def _industry(criteria: QualificationCriteria, ctx: _Context) -> FactorOutcome:
    return _positive_factor(
        key="industry_fit",
        label="Industry fit",
        weight=INDUSTRY_WEIGHT,
        terms=criteria.target_keywords,
        tlds=[],
        ctx=ctx,
    )


def _location(criteria: QualificationCriteria, ctx: _Context) -> FactorOutcome:
    if not criteria.target_locations and not criteria.target_tlds:
        return _not_applicable(
            "location_fit",
            "Location fit",
            LOCATION_WEIGHT,
            "No target locations or TLDs are configured.",
        )
    return _positive_factor(
        key="location_fit",
        label="Location fit",
        weight=LOCATION_WEIGHT,
        terms=criteria.target_locations,
        tlds=criteria.target_tlds,
        ctx=ctx,
    )


def _exclusion(criteria: QualificationCriteria, ctx: _Context) -> FactorOutcome:
    label = "Exclusion check"
    if not criteria.excluded_keywords:
        return _not_applicable(
            "exclusion_check", label, EXCLUSION_WEIGHT, "No excluded keywords are configured."
        )
    excluded = list(dict.fromkeys(criteria.excluded_keywords))
    evidence = [
        e for e in (_find(t, ctx.website + ctx.discovery) for t in excluded) if e is not None
    ]
    found = [item.signal for item in evidence]
    not_found = [term for term in excluded if term not in found]
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
                f"Excluded term(s) {_quote(found)} found in {_sources_of(evidence)}; "
                "this disqualifies the candidate."
            ),
        )
    if ctx.complete_research:
        return FactorOutcome(
            key="exclusion_check",
            label=label,
            weight=EXCLUSION_WEIGHT,
            result="matched",
            points=EXCLUSION_WEIGHT,
            disqualifying=False,
            signals_found=[],
            signals_not_found=not_found,
            evidence_level="website",
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
            f"No excluded terms appear in the available data, but {ctx.research_note}, "
            "so their absence cannot be confirmed."
        ),
    )


def _score(points: int, evaluated_weight: int) -> int:
    if evaluated_weight == 0:
        return 0
    # Integer round-half-up keeps the result exact and platform independent.
    return (200 * points + evaluated_weight) // (2 * evaluated_weight)


def _decide(
    factors: list[FactorOutcome],
    score: int,
    coverage: float,
    criteria: QualificationCriteria,
) -> tuple[QualificationStatus, str]:
    industry = next(f for f in factors if f.key == "industry_fit")
    if any(f.disqualifying for f in factors):
        return "unqualified", "an excluded term was found"
    if coverage < criteria.min_evidence_coverage:
        return "insufficient_evidence", (
            f"only {coverage:.0%} of the applicable factor weight could be evaluated "
            f"(minimum {criteria.min_evidence_coverage:.0%})"
        )
    if score < criteria.threshold:
        return "unqualified", f"score {score} is below the threshold {criteria.threshold}"
    if industry.result == "not_matched":
        return "unqualified", "industry fit, a required factor, is not met"
    if industry.result == "unknown":
        return "insufficient_evidence", (
            "industry fit, a required factor, could not be evaluated"
        )
    if industry.evidence_level == "discovery":
        return "preliminary", (
            f"score {score} meets the threshold {criteria.threshold}, but industry fit "
            "rests only on search-result data; research the company's website to confirm "
            "before saving"
        )
    return "qualified", f"score {score} meets the threshold {criteria.threshold}"


def evaluate(company: EnrichedCompany, criteria: QualificationCriteria) -> Evaluation:
    """Apply :data:`RULE_VERSION` rules to one enriched company."""
    ctx = _context(company)
    factors = [_industry(criteria, ctx), _location(criteria, ctx), _exclusion(criteria, ctx)]

    applicable = [f for f in factors if f.result != "not_applicable"]
    evaluated = [f for f in applicable if f.result in ("matched", "not_matched")]
    applicable_weight = sum(f.weight for f in applicable)
    evaluated_weight = sum(f.weight for f in evaluated)
    points = sum(f.points for f in evaluated)
    score = _score(points, evaluated_weight)
    coverage = round(evaluated_weight / applicable_weight, 4) if applicable_weight else 0.0
    status, decision = _decide(factors, score, coverage, criteria)

    lines = [
        f"{status.replace('_', ' ').capitalize()}: {decision}. Score {score}/100 from "
        f"{points} of {evaluated_weight} evaluated points; evidence coverage "
        f"{coverage:.0%}; profile '{criteria.profile}'; rules {RULE_VERSION}.",
    ]
    for f in factors:
        level = f", {f.evidence_level} evidence" if f.evidence_level else ""
        lines.append(
            f"- {f.label} ({f.result.replace('_', ' ')}{level}, {f.points}/{f.weight}): "
            f"{f.explanation}"
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
