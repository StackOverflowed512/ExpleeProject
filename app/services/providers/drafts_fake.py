"""Deterministic fake draft generator.

Used by tests and credential-free runs (``DRAFT_GENERATOR=fake``). It never
touches the network. By default it writes a draft from the prompt's
structured context; tests can instead supply raw model text (to exercise
:func:`parse_draft_output`) or an error to raise. Every call is recorded in
:attr:`FakeDraftGenerator.prompts`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.providers.drafts import (
    DraftGeneratorError,
    DraftPrompt,
    GeneratedDraft,
    parse_draft_output,
)

PROVIDER_NAME = "fake"
FAKE_MODEL = "fake-drafts-1"


def default_output(prompt: DraftPrompt) -> str:
    """Build model-style JSON text from the prompt's context."""
    company = prompt.context.get("company", {})
    campaign = prompt.context.get("campaign", {})
    label = company.get("domain") or "your team"
    product = campaign.get("product", "our product")
    return json.dumps(
        {
            "subject": f"{product} for {label}",
            "body": (
                f"Hello,\n\nI am reaching out about {product}. "
                f"{campaign.get('value_proposition', '')}\n\n"
                f"{campaign.get('call_to_action', '')}\n\nBest regards"
            ),
        }
    )


@dataclass
class FakeDraftGenerator:
    """In-memory generator with scripted behaviour."""

    name: str = PROVIDER_NAME
    model: str = FAKE_MODEL
    output: Callable[[DraftPrompt], str] = default_output
    error: DraftGeneratorError | None = None
    fail_on_call: int | None = None
    prompts: list[DraftPrompt] = field(default_factory=list)

    def generate(self, prompt: DraftPrompt) -> GeneratedDraft:
        self.prompts.append(prompt)
        if self.error is not None and (
            self.fail_on_call is None or len(self.prompts) == self.fail_on_call
        ):
            raise self.error
        subject, body = parse_draft_output(self.output(prompt))
        return GeneratedDraft(
            subject=subject,
            body=body,
            provider=self.name,
            model=self.model,
            metadata={"finish_reason": "stop"},
        )
