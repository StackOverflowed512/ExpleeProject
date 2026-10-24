"""Unit tests for campaign and draft-request validation. No database access."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.campaigns import MAX_CAMPAIGN_PROSPECTS, CampaignCreate
from app.schemas.drafts import GenerateDraftsRequest

VALID: dict[str, object] = {
    "name": "Q4 fleet operators",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
    "prospect_ids": [3, 1, 2],
}


def _campaign(**overrides: object) -> CampaignCreate:
    return CampaignCreate.model_validate(VALID | overrides)


def test_valid_campaign_keeps_order_and_defaults() -> None:
    campaign = _campaign(name="  Q4  ", description="   ")
    assert campaign.prospect_ids == [3, 1, 2]
    assert campaign.tone == "professional"
    assert campaign.name == "Q4"
    assert campaign.description is None
    assert campaign.sender_name is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"prospect_ids": []},
        {"prospect_ids": [1, 2, 1]},
        {"prospect_ids": [0]},
        {"prospect_ids": [-5]},
        {"prospect_ids": [2_147_483_648]},
        {"prospect_ids": ["abc"]},
        {"prospect_ids": list(range(1, MAX_CAMPAIGN_PROSPECTS + 2))},
        {"name": ""},
        {"name": "x" * 201},
        {"product": "x"},
        {"value_proposition": "short"},
        {"call_to_action": "Email me at alex@routewise.io"},
        {"value_proposition": "We help [Company] cut planning time a lot."},
        {"sender_name": "{{name}}"},
        {"target_audience": "Ops\x00leaders"},
        {"tone": "aggressive"},
        {"candidate_ids": [1]},
        {"website_url": "http://169.254.169.254/"},
    ],
)
def test_invalid_campaigns_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _campaign(**overrides)


def test_duplicate_prospect_message_names_the_ids() -> None:
    with pytest.raises(ValidationError, match="must not repeat: 1, 2"):
        _campaign(prospect_ids=[1, 2, 1, 2, 3])


@pytest.mark.parametrize("tone", ["professional", "friendly", "concise", "formal"])
def test_supported_tones(tone: str) -> None:
    assert _campaign(tone=tone).tone == tone


def test_generate_request_defaults_and_validation() -> None:
    assert GenerateDraftsRequest().saved_prospect_ids is None
    assert GenerateDraftsRequest().regenerate is False
    for bad in (
        {"saved_prospect_ids": []},
        {"saved_prospect_ids": [1, 1]},
        {"saved_prospect_ids": [0]},
        {"regenerate": "maybe"},
        {"send": True},
    ):
        with pytest.raises(ValidationError):
            GenerateDraftsRequest.model_validate(bad)
