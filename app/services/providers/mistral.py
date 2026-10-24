"""Mistral chat-completions adapter for outreach draft generation.

Contract (Mistral API, ``POST /v1/chat/completions``):

* Endpoint: ``https://api.mistral.ai/v1/chat/completions``.
* Authentication: ``Authorization: Bearer <api key>``.
* Body: ``model``, ``messages`` (system + user), ``temperature``,
  ``max_tokens``, ``random_seed``, and
  ``response_format={"type": "json_object"}`` (JSON mode; the prompt itself
  also asks for JSON, as the API requires).
* Response: ``{"id", "model", "choices": [{"message": {"content"},
  "finish_reason"}], "usage": {...}}``.

Verify the contract at https://docs.mistral.ai/api/ before relying on it in
production. Provider-specific request and response handling stays in this
module. The adapter never logs the API key, headers, prompts, or response
bodies, and retries only transient failures with bounded backoff.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.services.providers.drafts import (
    DraftGeneratorAuthenticationError,
    DraftGeneratorConfigurationError,
    DraftGeneratorError,
    DraftGeneratorRateLimitError,
    DraftGeneratorTimeoutError,
    DraftGeneratorUnavailableError,
    DraftOutputError,
    DraftPrompt,
    GeneratedDraft,
    parse_draft_output,
)

logger = logging.getLogger(__name__)

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
PROVIDER_NAME = "mistral"
RANDOM_SEED = 7
MAX_RETRY_AFTER_SECONDS = 5.0
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _text_content(content: object) -> str | None:
    """Return message content as text (plain string or list of text chunks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            chunk.get("text")
            for chunk in content
            if isinstance(chunk, dict) and chunk.get("type") == "text"
        ]
        if parts and all(isinstance(part, str) for part in parts):
            return "".join(parts)
    return None


class MistralDraftGenerator:
    """Draft generator backed by the Mistral chat-completions API.

    A missing key is accepted at construction so the application starts
    without credentials; :meth:`generate` raises
    :class:`DraftGeneratorConfigurationError` before any network call.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        client: httpx.Client | None = None,
    ) -> None:
        key = api_key.strip() if isinstance(api_key, str) else ""
        self._api_key = key or None
        self.model = model
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._client = client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MistralDraftGenerator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def generate(self, prompt: DraftPrompt) -> GeneratedDraft:
        if self._api_key is None:
            raise DraftGeneratorConfigurationError(
                "Mistral draft generation is selected but MISTRAL_API_KEY is not configured."
            )
        payload = self._request(prompt)
        return self._to_draft(payload)

    # -- HTTP ---------------------------------------------------------
    def _request(self, prompt: DraftPrompt) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": prompt.temperature,
            "max_tokens": prompt.max_output_tokens,
            "random_seed": RANDOM_SEED,
            "response_format": {"type": "json_object"},
        }

        last_error: DraftGeneratorError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    MISTRAL_CHAT_URL, headers=headers, json=body, timeout=self._timeout
                )
            except httpx.TimeoutException:
                last_error = DraftGeneratorTimeoutError("The draft generator timed out.")
                self._wait(attempt)
                continue
            except httpx.TransportError:
                last_error = DraftGeneratorUnavailableError(
                    "The draft generator could not be reached."
                )
                self._wait(attempt)
                continue

            status = response.status_code
            if status == 200:
                try:
                    parsed = response.json()
                except ValueError:
                    raise DraftOutputError(
                        "The draft generator returned a non-JSON response."
                    ) from None
                if not isinstance(parsed, dict):
                    raise DraftOutputError("The draft generator returned an unexpected payload.")
                return parsed
            if status in (401, 403):
                raise DraftGeneratorAuthenticationError(
                    "The draft generator rejected the configured credentials."
                )
            if status == 429:
                last_error = DraftGeneratorRateLimitError(
                    "The draft generator rate limit was exceeded."
                )
                self._wait(attempt, response)
                continue
            if status >= 500:
                last_error = DraftGeneratorUnavailableError(
                    "The draft generator is temporarily unavailable."
                )
                self._wait(attempt, response)
                continue
            # Other 4xx (invalid model, bad request): retrying will not help.
            logger.warning("Mistral rejected a draft request with HTTP %s", status)
            raise DraftGeneratorUnavailableError("The draft generator rejected the request.")

        assert last_error is not None
        raise last_error

    def _wait(self, attempt: int, response: httpx.Response | None = None) -> None:
        """Sleep before a retry, honoring a bounded ``Retry-After`` value."""
        if attempt >= self._max_retries:
            return
        delay = min(0.5 * (2**attempt), 4.0)
        if response is not None:
            raw = response.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw is not None else None
            except ValueError:
                retry_after = None
            if retry_after is not None:
                delay = max(delay, min(max(0.0, retry_after), MAX_RETRY_AFTER_SECONDS))
        time.sleep(delay)

    # -- parsing ------------------------------------------------------
    def _to_draft(self, payload: dict[str, Any]) -> GeneratedDraft:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise DraftOutputError("The draft generator returned no choices.")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason != "stop":
            # "length"/"model_length" mean a truncated draft; anything else
            # is not a complete answer either.
            raise DraftOutputError("The draft generator did not finish the draft.")
        message = choice.get("message")
        content = _text_content(message.get("content")) if isinstance(message, dict) else None
        if content is None:
            raise DraftOutputError("The draft generator returned no text.")

        subject, body = parse_draft_output(content)

        usage = payload.get("usage")
        metadata: dict[str, Any] = {"finish_reason": finish_reason}
        if isinstance(payload.get("id"), str):
            metadata["response_id"] = payload["id"][:100]
        if isinstance(usage, dict):
            metadata["usage"] = {
                key: usage[key]
                for key in _USAGE_FIELDS
                if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
            }
        reported_model = payload.get("model")
        model = self.model
        if isinstance(reported_model, str) and reported_model.strip():
            model = reported_model.strip()[:100]
        return GeneratedDraft(
            subject=subject,
            body=body,
            provider=PROVIDER_NAME,
            model=model,
            metadata=metadata,
        )
