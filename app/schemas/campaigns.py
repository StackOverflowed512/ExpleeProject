"""Campaign request and response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.prospects import ProspectSummary
from app.schemas.text_rules import contains_email, contains_placeholder

CampaignTone = Literal["professional", "friendly", "concise", "formal"]
MAX_CAMPAIGN_PROSPECTS = 50
MAX_DB_ID = 2_147_483_647
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ProspectIdList = Annotated[
    list[Annotated[int, Field(ge=1, le=MAX_DB_ID)]],
    Field(min_length=1, max_length=MAX_CAMPAIGN_PROSPECTS),
]


def _clean_text(value: object) -> object:
    """Strip outer whitespace and reject control characters."""
    if not isinstance(value, str):
        return value
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value.strip()


def _reject_unusable(value: str | None) -> str | None:
    # Drafts are rejected if they contain email addresses or unfilled
    # placeholders, so campaign text that would force them in is refused.
    if value is None:
        return value
    if contains_email(value):
        raise ValueError("must not contain email addresses")
    if contains_placeholder(value):
        raise ValueError("must not contain template placeholders such as [Name]")
    return value


class CampaignCreate(BaseModel):
    """Body of ``POST /campaigns``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Q4 fleet operators",
                    "description": "Logistics SaaS companies in Germany.",
                    "product": "RouteWise",
                    "value_proposition": (
                        "RouteWise cuts dispatch planning time by suggesting optimal "
                        "routes from live order data."
                    ),
                    "target_audience": "Operations leaders at logistics software companies",
                    "call_to_action": "Would a 20-minute call next week be useful?",
                    "tone": "professional",
                    "sender_name": "Alex",
                    "sender_company": "RouteWise",
                    "prospect_ids": [1, 2],
                }
            ]
        },
    )

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    product: str = Field(
        min_length=2, max_length=200, description="Product or service being promoted."
    )
    value_proposition: str = Field(min_length=10, max_length=1000)
    target_audience: str = Field(min_length=2, max_length=500)
    call_to_action: str = Field(min_length=2, max_length=300)
    tone: CampaignTone = "professional"
    sender_name: str | None = Field(default=None, max_length=100)
    sender_company: str | None = Field(default=None, max_length=200)
    prospect_ids: ProspectIdList = Field(
        description=(
            f"Saved prospect IDs (1-{MAX_CAMPAIGN_PROSPECTS}, no duplicates); their "
            "order is kept."
        )
    )

    @field_validator(
        "name",
        "description",
        "product",
        "value_proposition",
        "target_audience",
        "call_to_action",
        "sender_name",
        "sender_company",
        mode="before",
    )
    @classmethod
    def _clean(cls, value: object) -> object:
        return _clean_text(value)

    @field_validator("description", "sender_name", "sender_company")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator(
        "product",
        "value_proposition",
        "target_audience",
        "call_to_action",
        "sender_name",
        "sender_company",
    )
    @classmethod
    def _usable_in_drafts(cls, value: str | None) -> str | None:
        return _reject_unusable(value)

    @field_validator("prospect_ids")
    @classmethod
    def _no_duplicates(cls, value: list[int]) -> list[int]:
        duplicates = sorted({pid for pid in value if value.count(pid) > 1})
        if duplicates:
            raise ValueError(
                "prospect_ids must not repeat: " + ", ".join(str(pid) for pid in duplicates)
            )
        return value


class CampaignSummary(BaseModel):
    """A campaign without its prospect list."""

    id: int
    name: str
    description: str | None = None
    product: str
    tone: CampaignTone
    prospect_count: int
    created_at: datetime
    updated_at: datetime


class CampaignProspectItem(BaseModel):
    """A saved prospect inside a campaign, in selection order."""

    position: int
    added_at: datetime
    prospect: ProspectSummary


class CampaignDetail(BaseModel):
    """A campaign with its full configuration and prospects."""

    id: int
    name: str
    description: str | None = None
    product: str
    value_proposition: str
    target_audience: str
    call_to_action: str
    tone: CampaignTone
    sender_name: str | None = None
    sender_company: str | None = None
    created_at: datetime
    updated_at: datetime
    prospects: list[CampaignProspectItem]


class CampaignListResponse(BaseModel):
    """One page of campaigns, newest first."""

    items: list[CampaignSummary]
    total: int
    limit: int
    offset: int
