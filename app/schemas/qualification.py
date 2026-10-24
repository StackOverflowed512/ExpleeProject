"""Qualification criteria, factor results, and qualification records."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

QualificationStatus = Literal["qualified", "unqualified", "insufficient_evidence"]
FactorResult = Literal["matched", "not_matched", "unknown", "not_applicable"]
FactorKey = Literal["industry_fit", "location_fit", "exclusion_check"]
EvidenceSource = Literal[
    "candidate.name",
    "candidate.description",
    "candidate.domain",
    "research.page_title",
    "research.extracted_text",
]

MAX_TERMS = 25
MAX_TERM_LENGTH = 60
# Letters (any script), digits, spaces, and a few joiners used in market
# terms such as "e-commerce", "b2b", "c++", "r&d", or "st. gallen".
_TERM_PATTERN = re.compile(r"^[\w][\w\s\-.&+/']*$", re.UNICODE)
_TLD_PATTERN = re.compile(r"^(?:[a-z]{2,24}|xn--[a-z0-9-]{1,59})$")

DEFAULT_PROFILE = "default-b2b-software"
DEFAULT_TARGET_KEYWORDS = ("b2b", "saas", "software", "platform")
DEFAULT_THRESHOLD = 60
DEFAULT_MIN_EVIDENCE_COVERAGE = 0.5

_STATUS_DESCRIPTION = (
    "'qualified': enough evidence and score >= threshold. 'unqualified': an "
    "excluded term was found, or the evidence is sufficient but the score is "
    "below the threshold. 'insufficient_evidence': too few factors could be "
    "evaluated from sourced data to decide."
)


def _normalize_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalize_terms(values: object, field: str) -> object:
    """Normalize, validate, de-duplicate, and sort a list of terms.

    Sorting makes the stored criteria canonical, so equal profiles always
    serialize identically.
    """
    if not isinstance(values, list):
        return values
    terms: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{field} entries must be strings.")
        term = _normalize_term(raw)
        if not term:
            raise ValueError(f"{field} entries must not be blank.")
        if len(term) < 2 or len(term) > MAX_TERM_LENGTH:
            raise ValueError(
                f"{field} entries must be 2-{MAX_TERM_LENGTH} characters long."
            )
        if not _TERM_PATTERN.match(term):
            raise ValueError(
                f"{field} entries may contain only letters, digits, spaces, "
                "and - . & + / '."
            )
        terms.add(term)
    return sorted(terms)


TermList = Annotated[list[str], Field(max_length=MAX_TERMS)]


class QualificationCriteria(BaseModel):
    """A target-market profile that qualification rules are evaluated against.

    All term matching is case-insensitive and whole-word (multi-word phrases
    allowed). Terms are normalized (NFKC, case-folded, whitespace collapsed),
    de-duplicated, and sorted, so equivalent profiles are stored identically.
    """

    model_config = ConfigDict(
        extra="forbid",
        # Defaults go through the same normalization as caller input, so the
        # stored criteria are canonical either way.
        validate_default=True,
        json_schema_extra={
            "examples": [
                {
                    "profile": "berlin-b2b-saas",
                    "target_keywords": ["b2b", "saas"],
                    "target_locations": ["berlin", "germany"],
                    "target_tlds": ["de"],
                    "excluded_keywords": ["recruitment agency"],
                    "threshold": 60,
                    "min_evidence_coverage": 0.5,
                }
            ]
        },
    )

    profile: str = Field(
        default=DEFAULT_PROFILE,
        min_length=1,
        max_length=60,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$",
        description="Label recorded with each run for auditing.",
    )
    target_keywords: TermList = Field(
        default_factory=lambda: list(DEFAULT_TARGET_KEYWORDS),
        min_length=1,
        description="Industry or market terms; at least one is required.",
    )
    target_locations: TermList = Field(
        default_factory=list,
        description="Location terms (cities, regions, countries). Empty = not applicable.",
    )
    target_tlds: TermList = Field(
        default_factory=list,
        description="Country-code or other TLDs (without dot) that indicate a target location.",
    )
    excluded_keywords: TermList = Field(
        default_factory=list,
        description="Terms that disqualify a candidate when found. Empty = not applicable.",
    )
    threshold: int = Field(
        default=DEFAULT_THRESHOLD,
        ge=1,
        le=100,
        description="Minimum score (0-100) for 'qualified'.",
    )
    min_evidence_coverage: float = Field(
        default=DEFAULT_MIN_EVIDENCE_COVERAGE,
        gt=0,
        le=1,
        description=(
            "Minimum share of applicable factor weight that must be evaluable "
            "from sourced data; below it the status is 'insufficient_evidence'."
        ),
    )

    @field_validator("profile", mode="before")
    @classmethod
    def _strip_profile(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("target_keywords", mode="before")
    @classmethod
    def _norm_keywords(cls, value: object) -> object:
        return _normalize_terms(value, "target_keywords")

    @field_validator("target_locations", mode="before")
    @classmethod
    def _norm_locations(cls, value: object) -> object:
        return _normalize_terms(value, "target_locations")

    @field_validator("excluded_keywords", mode="before")
    @classmethod
    def _norm_excluded(cls, value: object) -> object:
        return _normalize_terms(value, "excluded_keywords")

    @field_validator("target_tlds", mode="before")
    @classmethod
    def _norm_tlds(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        tlds: set[str] = set()
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("target_tlds entries must be strings.")
            tld = raw.strip().casefold().lstrip(".")
            if not _TLD_PATTERN.match(tld):
                raise ValueError(
                    "target_tlds entries must be a final domain label such as "
                    "'de' or 'io' (letters only, 2-24 characters, or punycode)."
                )
            tlds.add(tld)
        return sorted(tlds)

    @field_validator("min_evidence_coverage")
    @classmethod
    def _round_coverage(cls, value: float) -> float:
        return round(value, 4)


class QualifyRequest(BaseModel):
    """Body of ``POST /companies/{id}/qualify``. Omit ``criteria`` for defaults."""

    model_config = ConfigDict(extra="forbid")

    criteria: QualificationCriteria = Field(default_factory=QualificationCriteria)


class Evidence(BaseModel):
    """Where a signal was found. Excerpts are third-party text, not facts."""

    signal: str
    source: EvidenceSource
    source_url: str | None = Field(
        default=None,
        description="Discovery source URL or research final/requested URL.",
    )
    research_id: int | None = None
    excerpt: str = Field(description="Text around the match (at most ~160 characters).")


class FactorOutcome(BaseModel):
    """One scoring factor and how it affected the score."""

    key: FactorKey
    label: str
    weight: int = Field(description="Points available when the factor is evaluated.")
    result: FactorResult
    points: int = Field(description="Points awarded (weight when matched, else 0).")
    disqualifying: bool = Field(
        description="True when this factor alone makes the candidate unqualified."
    )
    signals_found: list[str] = Field(description="Criteria terms found in sourced data.")
    signals_not_found: list[str] = Field(description="Criteria terms not found.")
    evidence: list[Evidence]
    explanation: str


class QualificationRecord(BaseModel):
    """A persisted qualification run."""

    id: int
    candidate_id: int
    research_id: int | None = None
    rule_version: str
    criteria: QualificationCriteria
    status: QualificationStatus = Field(description=_STATUS_DESCRIPTION)
    score: int = Field(ge=0, le=100)
    threshold: int
    evidence_coverage: float = Field(ge=0, le=1)
    matched_signals: list[str] = Field(
        description="Target keywords and locations found (positive signals)."
    )
    missing_signals: list[str] = Field(
        description="Target keywords and locations not found in sourced data."
    )
    disqualifying_signals: list[str] = Field(
        description="Excluded terms found in sourced data."
    )
    factors: list[FactorOutcome]
    rationale: str
    qualified_at: datetime
    created_at: datetime


class QualificationSummary(BaseModel):
    """Compact view of a qualification, used in prospect listings."""

    id: int
    status: QualificationStatus
    score: int
    threshold: int
    evidence_coverage: float
    rule_version: str
    profile: str
    qualified_at: datetime


class QualificationHistoryResponse(BaseModel):
    """Qualification runs for one candidate, newest first."""

    candidate_id: int
    total: int = Field(description="Total runs stored for the candidate.")
    latest: QualificationRecord | None = None
    history: list[QualificationRecord]
