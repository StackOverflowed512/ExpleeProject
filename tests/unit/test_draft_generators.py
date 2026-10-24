"""Unit tests for draft output validation, the Mistral adapter, and the fake.

All HTTP is served by :class:`httpx.MockTransport`; the real Mistral API is
never called and no credentials are needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.outreach_prompt import CampaignBrief, ProspectFacts, build_prompt
from app.services.providers import (
    DraftGenerator,
    FakeDraftGenerator,
    MistralDraftGenerator,
    build_draft_generator,
)
from app.services.providers.drafts import (
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    DraftGeneratorAuthenticationError,
    DraftGeneratorConfigurationError,
    DraftGeneratorRateLimitError,
    DraftGeneratorTimeoutError,
    DraftGeneratorUnavailableError,
    DraftOutputError,
    parse_draft_output,
)
from app.services.providers.mistral import MISTRAL_CHAT_URL

Handler = Callable[[httpx.Request], httpx.Response]
SECRET = "mistral-test-key-123"
BODY = "Hello,\n\nRouteWise helps freight teams plan routes faster. Open to a short call?"
PROMPT = build_prompt(
    CampaignBrief(
        product="RouteWise",
        value_proposition="Faster dispatch planning for freight teams.",
        target_audience="Logistics companies",
        call_to_action="Open to a short call?",
        tone="professional",
    ),
    ProspectFacts(company_name="Acme", domain="acme.com"),
)


def _content(subject: str = "RouteWise for acme.com", body: str = BODY) -> str:
    return json.dumps({"subject": subject, "body": body})


def _completion(content: object = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "cmpl-123",
        "object": "chat.completion",
        "model": "mistral-small-2501",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": _content() if content is None else content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 410, "completion_tokens": 95, "total_tokens": 505},
    }
    payload.update(overrides)
    return payload


def _generator(
    handler: Handler, *, api_key: str | None = SECRET, max_retries: int = 0
) -> MistralDraftGenerator:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MistralDraftGenerator(
        api_key, model="mistral-small-latest", max_retries=max_retries, client=client
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("app.services.providers.mistral.time.sleep", delays.append)
    return delays


# -- output validation ----------------------------------------------------------
def test_valid_output_is_parsed_and_normalized() -> None:
    subject, body = parse_draft_output(_content(subject="  RouteWise \n for   Acme  "))
    assert subject == "RouteWise for Acme"
    assert body == BODY


def test_code_fenced_json_is_accepted() -> None:
    assert parse_draft_output(f"```json\n{_content()}\n```")[1] == BODY


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Sure! Here is your email: Hello there",
        "[1, 2]",
        json.dumps({"subject": "Hi"}),
        json.dumps({"body": BODY}),
        json.dumps({"subject": "", "body": BODY}),
        json.dumps({"subject": "Hi", "body": "   "}),
        json.dumps({"subject": 5, "body": BODY}),
        json.dumps({"subject": "Hi", "body": "Too short."}),
        json.dumps({"subject": "x" * (MAX_SUBJECT_LENGTH + 1), "body": BODY}),
        json.dumps({"subject": "Hi", "body": "y" * (MAX_BODY_LENGTH + 1)}),
        json.dumps({"subject": "Hi", "body": BODY + " Reach me at alex@routewise.io."}),
        json.dumps({"subject": "Hi [Name]", "body": BODY}),
        json.dumps({"subject": "Hi", "body": BODY + " Best, {{sender_name}}"}),
        json.dumps({"subject": "Hi", "body": "Dear <FIRST_NAME>, " + BODY}),
    ],
)
def test_invalid_output_is_rejected(raw: str) -> None:
    with pytest.raises(DraftOutputError):
        parse_draft_output(raw)


# -- Mistral adapter -----------------------------------------------------------------
def test_request_shape_and_successful_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion())

    draft = _generator(handler).generate(PROMPT)

    (request,) = seen
    assert str(request.url) == MISTRAL_CHAT_URL
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert body["model"] == "mistral-small-latest"
    assert body["messages"] == [
        {"role": "system", "content": PROMPT.system},
        {"role": "user", "content": PROMPT.user},
    ]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == pytest.approx(0.3)
    assert body["max_tokens"] == 800
    assert isinstance(body["random_seed"], int)

    assert (draft.subject, draft.body) == ("RouteWise for acme.com", BODY)
    assert draft.provider == "mistral"
    assert draft.model == "mistral-small-2501"
    assert draft.metadata == {
        "finish_reason": "stop",
        "response_id": "cmpl-123",
        "usage": {"prompt_tokens": 410, "completion_tokens": 95, "total_tokens": 505},
    }


def test_text_chunk_content_is_supported() -> None:
    chunks = [{"type": "text", "text": _content()[:20]}, {"type": "text", "text": _content()[20:]}]
    draft = _generator(lambda r: httpx.Response(200, json=_completion(chunks))).generate(PROMPT)
    assert draft.body == BODY


def test_missing_key_fails_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    for key in (None, "", "   "):
        with pytest.raises(DraftGeneratorConfigurationError) as caught:
            _generator(handler, api_key=key).generate(PROMPT)
        assert caught.value.code == "provider_not_configured"
        assert caught.value.http_status == 503


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, DraftGeneratorAuthenticationError),
        (403, DraftGeneratorAuthenticationError),
        (400, DraftGeneratorUnavailableError),
        (404, DraftGeneratorUnavailableError),
        (422, DraftGeneratorUnavailableError),
    ],
)
def test_permanent_http_errors_are_not_retried(
    status: int, error: type[Exception], no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"message": f"secret detail {SECRET}"})

    with pytest.raises(error) as caught:
        _generator(handler, max_retries=2).generate(PROMPT)
    assert len(calls) == 1
    assert SECRET not in str(caught.value)
    assert "secret detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (429, DraftGeneratorRateLimitError, "provider_rate_limited"),
        (500, DraftGeneratorUnavailableError, "provider_unavailable"),
        (503, DraftGeneratorUnavailableError, "provider_unavailable"),
    ],
)
def test_transient_http_errors_are_retried_then_mapped(
    status: int, error: type[Exception], code: str, no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, headers={"Retry-After": "60"})

    with pytest.raises(error) as caught:
        _generator(handler, max_retries=2).generate(PROMPT)
    assert len(calls) == 3
    assert caught.value.code == code  # type: ignore[attr-defined]
    assert no_sleep and max(no_sleep) <= 5.0  # Retry-After is capped


def test_retry_recovers_after_transient_failure(no_sleep: list[float]) -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, json=_completion())])
    draft = _generator(lambda r: next(responses), max_retries=1).generate(PROMPT)
    assert draft.subject == "RouteWise for acme.com"


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (httpx.ReadTimeout("slow"), DraftGeneratorTimeoutError),
        (httpx.ConnectTimeout("slow"), DraftGeneratorTimeoutError),
        (httpx.ConnectError("down"), DraftGeneratorUnavailableError),
    ],
)
def test_transport_failures_are_mapped(
    exception: Exception, error: type[Exception], no_sleep: list[float]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(error):
        _generator(handler, max_retries=1).generate(PROMPT)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json=_completion(choices=[])),
        httpx.Response(200, json=_completion(choices=["oops"])),
        httpx.Response(200, json=_completion() | {"choices": [{"finish_reason": "stop"}]}),
        httpx.Response(
            200,
            json=_completion()
            | {"choices": [{"message": {"content": _content()}, "finish_reason": "length"}]},
        ),
        httpx.Response(200, json=_completion("plain text, not JSON")),
        httpx.Response(200, json=_completion(json.dumps({"subject": "", "body": BODY}))),
        httpx.Response(
            200, json=_completion(json.dumps({"subject": "Hi", "body": "Hi [Name]! " + BODY}))
        ),
    ],
)
def test_malformed_or_incomplete_responses_are_rejected(response: httpx.Response) -> None:
    with pytest.raises(DraftOutputError) as caught:
        _generator(lambda r: response).generate(PROMPT)
    assert caught.value.code == "provider_invalid_output"


def test_unexpected_usage_fields_are_not_stored() -> None:
    payload = _completion(usage={"prompt_tokens": 3, "raw": "x", "total_tokens": True})
    draft = _generator(lambda r: httpx.Response(200, json=payload)).generate(PROMPT)
    assert draft.metadata["usage"] == {"prompt_tokens": 3}


def test_owned_client_is_closed_and_injected_client_is_not() -> None:
    owned = MistralDraftGenerator(SECRET, model="m")
    owned.close()
    assert owned._client.is_closed
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    MistralDraftGenerator(SECRET, model="m", client=client).close()
    assert not client.is_closed
    client.close()


def test_owned_client_ignores_proxy_environment() -> None:
    generator = MistralDraftGenerator(SECRET, model="m")
    assert generator._client.trust_env is False
    generator.close()


def test_generator_repr_never_contains_the_key() -> None:
    generator = MistralDraftGenerator(SECRET, model="m")
    assert SECRET not in repr(generator)
    generator.close()


# -- fake generator, selection, settings -----------------------------------------------
def test_fake_generator_is_deterministic_and_records_prompts() -> None:
    fake = FakeDraftGenerator()
    first = fake.generate(PROMPT)
    second = fake.generate(PROMPT)
    assert first == second
    assert first.provider == "fake"
    assert "acme.com" in first.subject
    assert fake.prompts == [PROMPT, PROMPT]


def test_fake_generator_can_fail_on_a_given_call() -> None:
    fake = FakeDraftGenerator(error=DraftGeneratorTimeoutError("slow"), fail_on_call=2)
    fake.generate(PROMPT)
    with pytest.raises(DraftGeneratorTimeoutError):
        fake.generate(PROMPT)


def test_fake_generator_validates_scripted_output() -> None:
    fake = FakeDraftGenerator(output=lambda prompt: "not json")
    with pytest.raises(DraftOutputError):
        fake.generate(PROMPT)


def test_adapters_satisfy_the_protocol() -> None:
    assert isinstance(FakeDraftGenerator(), DraftGenerator)
    with MistralDraftGenerator(None, model="m") as generator:
        assert isinstance(generator, DraftGenerator)


def test_builder_selects_configured_generator() -> None:
    fake = build_draft_generator(Settings(_env_file=None, draft_generator="fake"))
    assert isinstance(fake, FakeDraftGenerator)
    real = build_draft_generator(
        Settings(_env_file=None, mistral_api_key=SECRET, mistral_model="open-mistral-nemo")
    )
    assert isinstance(real, MistralDraftGenerator)
    assert real.model == "open-mistral-nemo"
    real.close()


def test_mistral_settings_defaults_and_secret_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.draft_generator == "mistral"
    assert settings.mistral_api_key is None
    assert settings.mistral_model == "mistral-small-latest"
    monkeypatch.setenv("MISTRAL_API_KEY", f"  {SECRET}  ")
    configured = Settings(_env_file=None)
    assert configured.mistral_api_key is not None
    assert configured.mistral_api_key.get_secret_value() == SECRET
    assert SECRET not in repr(configured)
    monkeypatch.setenv("MISTRAL_API_KEY", "   ")
    assert Settings(_env_file=None).mistral_api_key is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("draft_generator", "openai"),
        ("mistral_model", ""),
        ("mistral_model", "bad model"),
        ("mistral_timeout_seconds", 0),
        ("mistral_timeout_seconds", 121),
        ("mistral_max_retries", 4),
    ],
)
def test_invalid_generator_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]
