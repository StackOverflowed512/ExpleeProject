"""Draft generator interface, result contract, errors, and output validation.

Outreach business logic depends only on :class:`DraftGenerator`. Adapters
(``mistral``, ``drafts_fake``) are selected in
:func:`app.services.providers.build_draft_generator`.

Every adapter must turn the model's raw text into a :class:`GeneratedDraft`
through :func:`parse_draft_output`, so all providers share one strict
definition of a valid draft: a JSON object with a non-empty ``subject`` and
``body`` within length limits, containing no email addresses and no
unfilled placeholders such as ``[Name]``. Anything else is rejected with
:class:`DraftOutputError` and nothing is persisted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.schemas.text_rules import contains_email, contains_placeholder

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 5000
MIN_BODY_LENGTH = 40


# -- errors -------------------------------------------------------------
class DraftGeneratorError(Exception):
    """Base class for draft generator failures.

    ``code`` is a stable snake_case identifier surfaced in the API error
    envelope, matching the codes used by search providers. Messages never
    include credentials or raw provider bodies.
    """

    code = "provider_error"
    http_status = 502


class DraftGeneratorConfigurationError(DraftGeneratorError):
    """The selected generator is missing required configuration."""

    code = "provider_not_configured"
    http_status = 503


class DraftGeneratorAuthenticationError(DraftGeneratorError):
    """The provider rejected the configured credentials."""

    code = "provider_authentication_failed"
    http_status = 502


class DraftGeneratorRateLimitError(DraftGeneratorError):
    """The provider rate limit was exceeded after retries."""

    code = "provider_rate_limited"
    http_status = 503


class DraftGeneratorTimeoutError(DraftGeneratorError):
    """The provider did not respond within the configured timeout."""

    code = "provider_timeout"
    http_status = 504


class DraftGeneratorUnavailableError(DraftGeneratorError):
    """The provider failed, rejected the request, or was unreachable."""

    code = "provider_unavailable"
    http_status = 502


class DraftOutputError(DraftGeneratorError):
    """The provider answered, but not with a valid draft."""

    code = "provider_invalid_output"
    http_status = 502


# -- contract -------------------------------------------------------------
@dataclass(frozen=True)
class DraftPrompt:
    """A fully built, versioned prompt.

    ``context`` is the structured data the prompt was built from; adapters
    must not send it separately (it is already embedded in ``user``). The
    fake generator uses it to produce deterministic drafts.
    """

    version: str
    system: str
    user: str
    context: dict[str, Any]
    temperature: float = 0.3
    max_output_tokens: int = 800


@dataclass(frozen=True)
class GeneratedDraft:
    """A validated draft plus safe generation metadata.

    ``metadata`` holds provider facts useful for auditing (response ID,
    finish reason, token usage, model reported by the provider); it never
    contains the raw response body or credentials.
    """

    subject: str
    body: str
    provider: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DraftGenerator(Protocol):
    """Contract every outreach draft generator must satisfy."""

    name: str
    model: str

    def generate(self, prompt: DraftPrompt) -> GeneratedDraft:
        """Return one validated draft for ``prompt``.

        Implementations must raise a :class:`DraftGeneratorError` subclass on
        failure, must validate output with :func:`parse_draft_output`, and
        must never send email or perform any action besides generation.
        """
        ...


# -- output validation ---------------------------------------------------
def _clean(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise DraftOutputError(f"The draft {field_name} is missing or not text.")
    cleaned = value.replace("\r\n", "\n").strip()
    if not cleaned:
        raise DraftOutputError(f"The draft {field_name} is empty.")
    return cleaned


def parse_draft_output(raw: str) -> tuple[str, str]:
    """Validate raw model text and return ``(subject, body)``.

    Accepts a JSON object, optionally wrapped in a Markdown code fence.
    Raises :class:`DraftOutputError` for anything that is not a complete,
    safe draft.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise DraftOutputError("The provider returned an empty draft.")
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except ValueError:
        raise DraftOutputError("The provider returned a draft that is not JSON.") from None
    if not isinstance(payload, dict):
        raise DraftOutputError("The provider returned an unexpected draft structure.")

    subject = " ".join(_clean(payload.get("subject"), "subject").split())
    body = _clean(payload.get("body"), "body")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise DraftOutputError("The draft subject is too long.")
    if len(body) > MAX_BODY_LENGTH:
        raise DraftOutputError("The draft body is too long.")
    if len(body) < MIN_BODY_LENGTH:
        raise DraftOutputError("The draft body is too short to be a usable message.")
    for part in (subject, body):
        if contains_email(part):
            raise DraftOutputError("The draft contains an email address that was not provided.")
        if contains_placeholder(part):
            raise DraftOutputError("The draft contains an unfilled placeholder.")
    return subject, body
