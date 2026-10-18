"""Discovery request and response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator


class DiscoveryRequest(BaseModel):
    """Input for a company discovery search."""

    query: str = Field(
        min_length=2,
        max_length=300,
        description="Natural-language search query.",
        examples=["B2B SaaS companies hiring sales engineers in Berlin"],
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of provider results to process (1-10).",
    )

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    model_config = {
        "json_schema_extra": {
            "examples": [{"query": "B2B SaaS companies in Berlin", "limit": 5}]
        }
    }


class CandidateSummary(BaseModel):
    """A persisted company candidate as returned by discovery."""

    id: int
    name: str
    website_url: str | None = None
    domain: str | None = None
    description: str | None = None
    provider: str
    provider_result_id: str | None = None
    source_url: str
    discovered_at: datetime
    updated_at: datetime


class DiscoveryCounts(BaseModel):
    """Outcome counters for one discovery request."""

    returned: int = Field(description="Candidates returned in this response.")
    created: int = Field(description="Candidates inserted in this search.")
    updated: int = Field(
        description="Existing candidates that gained previously missing fields."
    )
    skipped: int = Field(
        description="Provider results dropped because their URL was invalid."
    )


class DiscoveryResponse(BaseModel):
    """Normalized, persisted discovery results."""

    query: str
    provider: str
    requested_limit: int
    results: list[CandidateSummary]
    counts: DiscoveryCounts
