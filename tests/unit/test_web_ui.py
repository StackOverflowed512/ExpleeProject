"""Unit tests for the web interface building blocks (no database, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from starlette.requests import Request

from app.config import Settings
from app.schemas.drafts import ApprovalRecord, DraftDetail, SendAttemptRecord
from app.services.approvals import content_sha256
from app.services.delivery import STALE_SENDING_AFTER
from app.services.providers.email_fake import FakeEmailProvider
from app.web import csrf, presentation
from app.web.forms import (
    MAX_FORM_BYTES,
    FormError,
    field_errors,
    read_form,
    split_terms,
)
from app.web.rendering import (
    STATIC_DIR,
    TEMPLATES_DIR,
    build_templates,
    is_safe_ui_path,
    redirect,
)

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def _request(
    body: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
    cookies: str | None = None,
    scheme: str = "http",
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    raw_headers.append((b"host", b"127.0.0.1:8000"))
    if cookies:
        raw_headers.append((b"cookie", cookies.encode()))
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": scheme,
        "path": "/ui/x",
        "raw_path": b"/ui/x",
        "query_string": b"",
        "headers": raw_headers,
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope, receive)


FORM = {"content-type": "application/x-www-form-urlencoded"}


# -- CSRF primitives -------------------------------------------------------------------
def test_tokens_are_bound_to_secret_and_nonce() -> None:
    secret, other = csrf.new_secret(), csrf.new_secret()
    nonce = csrf.new_nonce()
    assert csrf.valid_nonce(nonce)
    assert csrf.token_for(secret, nonce) == csrf.token_for(secret, nonce)
    assert csrf.token_for(secret, nonce) != csrf.token_for(other, nonce)
    assert csrf.token_for(secret, nonce) != csrf.token_for(secret, csrf.new_nonce())


@pytest.mark.parametrize("value", [None, "", "short", "A" * 42, "A" * 44, "A" * 42 + "!"])
def test_malformed_nonces_are_rejected(value: str | None) -> None:
    assert not csrf.valid_nonce(value)


def test_verify_requires_a_matching_token() -> None:
    secret = csrf.new_secret()
    nonce = csrf.new_nonce()
    request = _request(cookies=f"{csrf.COOKIE_NAME}={nonce}")
    csrf.verify(request, secret, csrf.token_for(secret, nonce))
    for submitted in (None, "", "forged", csrf.token_for(csrf.new_secret(), nonce)):
        with pytest.raises(csrf.CsrfError):
            csrf.verify(request, secret, submitted)
    with pytest.raises(csrf.CsrfError):
        csrf.verify(_request(), secret, csrf.token_for(secret, nonce))


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, True),
        ({"origin": "http://127.0.0.1:8000"}, True),
        ({"origin": "HTTP://127.0.0.1:8000"}, True),
        ({"sec-fetch-site": "same-origin"}, True),
        ({"sec-fetch-site": "none"}, True),
        ({"origin": "http://localhost:8000"}, False),
        ({"origin": "http://127.0.0.1:3000"}, False),
        ({"origin": "null"}, False),
        ({"sec-fetch-site": "cross-site"}, False),
        ({"sec-fetch-site": "same-site"}, False),
    ],
)
def test_origin_checks(headers: dict[str, str], allowed: bool) -> None:
    request = _request(headers=headers)
    if allowed:
        csrf.check_origin(request)
    else:
        with pytest.raises(csrf.CsrfError):
            csrf.check_origin(request)


# -- form parsing ------------------------------------------------------------------------
async def test_read_form_parses_repeated_fields() -> None:
    form = await read_form(
        _request(b"prospect_ids=1&prospect_ids=2&name=Q4+%26+more", headers=FORM),
        allowed=("prospect_ids", "name"),
    )
    assert form.getlist("prospect_ids") == ["1", "2"]
    assert form.get("name") == "Q4 & more"
    assert form.get("missing") is None and not form.has("missing")


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        (b'{"a": 1}', {"content-type": "application/json"}, 415),
        (b"a=1", {"content-type": "multipart/form-data; boundary=x"}, 415),
        (b"name=\xff", FORM, 400),
        (b"x=" + b"a" * (MAX_FORM_BYTES + 1), FORM, 413),
        (b"x=1", FORM | {"content-length": str(MAX_FORM_BYTES + 1)}, 413),
        (b"subject=hi", FORM, 422),
    ],
    ids=["json", "multipart", "bad-utf8", "oversize", "declared-oversize", "unexpected-field"],
)
async def test_read_form_rejects_bad_input(
    body: bytes, headers: dict[str, str], status: int
) -> None:
    with pytest.raises(FormError) as caught:
        await read_form(_request(body, headers=headers), allowed=("name",))
    assert caught.value.status_code == status


def test_split_terms_and_field_errors() -> None:
    assert split_terms(" b2b, saas ,\n logistics,, ") == ["b2b", "saas", "logistics"]
    assert split_terms(None) == []
    from pydantic import BaseModel, ValidationError, field_validator

    class Model(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def _positive(cls, v: int) -> int:
            if v < 0:
                raise ValueError("must be positive")
            return v

    with pytest.raises(ValidationError) as caught:
        Model(value=-1)
    assert field_errors(caught.value) == {"value": "must be positive"}


# -- redirects -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "safe"),
    [
        ("/ui", True),
        ("/ui/drafts/1", True),
        ("/uix", False),
        ("//evil.example/ui", False),
        ("/ui//evil.example", False),
        ("https://evil.example/ui", False),
        ("/ui/\\evil", False),
        ("/ui/drafts?next=//evil", False),
        ("/ui/drafts#x", False),
        ("/ui/dr afts", False),
        ("/ui/\nSet-Cookie:x", False),
        ("/docs", False),
    ],
)
def test_only_internal_ui_paths_are_redirect_targets(path: str, safe: bool) -> None:
    assert is_safe_ui_path(path) is safe


def test_redirect_refuses_unsafe_targets_and_unknown_notices() -> None:
    assert redirect("/ui/drafts/1", notice="approved").headers["location"] == (
        "/ui/drafts/1?notice=approved"
    )
    assert redirect("/ui/drafts/1").status_code == 303
    with pytest.raises(ValueError):
        redirect("//evil.example")
    with pytest.raises(ValueError):
        redirect("/ui/drafts/1", notice="<script>")


# -- presentation ----------------------------------------------------------------------------
def test_status_views_always_have_text_labels() -> None:
    uncertain = presentation.status_view("draft_state", "delivery_uncertain")
    assert uncertain.label == "Delivery uncertain"
    assert presentation.status_view("draft_state", "brand_new").label == "Brand new"
    assert presentation.status_view("draft_state", None).label == "None"
    for kind, values in presentation._STATUSES.items():
        for value, view in values.items():
            assert view.label.strip(), (kind, value)
            assert view.tone in {"neutral", "info", "success", "warning", "danger"}


def test_failure_text_and_notices() -> None:
    assert "Nothing was sent" in presentation.failure_text("email_timeout")
    assert presentation.failure_text("something_new") == "Something new."
    assert presentation.failure_text(None) == ""
    assert presentation.notice_for("sent") is not None
    assert presentation.notice_for("<script>") is None
    assert presentation.notice_for(None) is None


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_delivery_mode_reports_every_blocker() -> None:
    fake = FakeEmailProvider()
    real = FakeEmailProvider(delivers_email=True)
    safe = presentation.delivery_mode(_settings(), fake)
    assert not safe.real_enabled and len(safe.blockers) == 3
    fake_only = presentation.delivery_mode(
        _settings(email_dry_run=False, email_sender="a@b.example"), fake
    )
    assert not fake_only.real_enabled
    assert any("only simulates" in reason for reason in fake_only.blockers)
    live = presentation.delivery_mode(
        _settings(email_dry_run=False, email_sender="a@b.example"), real
    )
    assert live.real_enabled and live.blockers == ()


def _detail(state: str, *, approved: bool = True, tampered: bool = False,
            attempts: list[SendAttemptRecord] | None = None) -> DraftDetail:
    subject, body = "Hello", "Body text"
    approval = None
    if approved:
        digest = content_sha256(
            draft_id=1, revision=1, recipient="ops@acme.com", subject=subject, body=body
        )
        approval = ApprovalRecord(
            id=1,
            draft_id=1,
            draft_revision=1,
            recipient_email="ops@acme.com",
            content_sha256="0" * 64 if tampered else digest,
            approved_by="local-operator",
            approved_at=NOW,
            created_at=NOW,
        )
    return DraftDetail(
        id=1,
        campaign_id=1,
        saved_prospect_id=1,
        revision=1,
        state=state,  # type: ignore[arg-type]
        subject=subject,
        body=body,
        prompt_version="v1",
        prompt_sha256="a" * 64,
        provider="fake",
        model="fake-1",
        generation_metadata={},
        provenance={},
        generated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        approval=approval,
        send_attempts=attempts or [],
    )


def _claim(started_at: datetime) -> SendAttemptRecord:
    return SendAttemptRecord(
        id=1, draft_id=1, approval_id=1, mode="real", provider="smtp",
        outcome="in_progress", started_at=started_at,
    )


def test_draft_actions_follow_the_state_machine() -> None:
    pending = presentation.draft_actions(_detail("pending", approved=False), NOW)
    assert pending.can_approve and not pending.can_send and pending.approval_matches is None

    approved = presentation.draft_actions(_detail("approved"), NOW)
    assert approved.can_send and approved.approval_matches and not approved.can_approve

    tampered = presentation.draft_actions(_detail("approved", tampered=True), NOW)
    assert not tampered.can_send and tampered.approval_matches is False

    uncertain = presentation.draft_actions(_detail("delivery_uncertain"), NOW)
    assert uncertain.can_resolve and not uncertain.can_send

    recent = presentation.draft_actions(_detail("sending", attempts=[_claim(NOW)]), NOW)
    assert recent.resolve_wait and not recent.can_resolve

    stale_start = NOW - STALE_SENDING_AFTER - timedelta(seconds=1)
    stale = presentation.draft_actions(_detail("sending", attempts=[_claim(stale_start)]), NOW)
    assert stale.can_resolve and not stale.resolve_wait

    sent = presentation.draft_actions(_detail("sent"), NOW)
    assert not (sent.can_send or sent.can_resolve or sent.can_approve)


# -- templates and assets -----------------------------------------------------------------------
def test_every_template_compiles() -> None:
    env = build_templates().env
    names = sorted(
        path.relative_to(TEMPLATES_DIR).as_posix() for path in TEMPLATES_DIR.rglob("*.html")
    )
    assert "base.html" in names and "drafts/detail.html" in names
    for name in names:
        env.get_template(name)
    assert env.autoescape is not False


def test_static_assets_are_present() -> None:
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    assert css.startswith("/*! tailwindcss v4")
    for component in (".btn-primary", ".badge-danger", ".alert-warning", ".sr-only"):
        assert component in css
    htmx = (STATIC_DIR / "vendor" / "htmx.min.js").read_text(encoding="utf-8")
    assert 'version:"2.0.10"' in htmx


# -- PR #11 review follow-ups ---------------------------------------------------------------
def test_research_notice_falls_back_for_unknown_outcomes() -> None:
    assert presentation.research_notice("success", reused=False) == "research_success"
    assert presentation.research_notice("failed", reused=False) == "research_failed"
    assert presentation.research_notice("success", reused=True) == "research_reused"
    assert presentation.research_notice("blocked", reused=False) == "research_done"
    assert redirect("/ui/companies/1", notice="research_done").status_code == 303


def test_draft_errors_resolve_subclasses() -> None:
    from app.routers.drafts import draft_error
    from app.services.delivery import DraftNotApprovedError
    from app.services.providers.email import EmailTimeoutError

    class StaleApprovalError(DraftNotApprovedError):
        pass

    assert draft_error(StaleApprovalError("x")) == ("draft_not_approved", 409)
    assert draft_error(DraftNotApprovedError("x")) == ("draft_not_approved", 409)
    assert draft_error(EmailTimeoutError("x")) == ("email_timeout", 504)
    with pytest.raises(KeyError):
        draft_error(RuntimeError("x"))


def test_build_css_takes_the_executable_as_an_option(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts import build_css

    assert build_css.main(["--tailwind", "does-not-exist.exe"]) == 1
    assert "--tailwind PATH" in capsys.readouterr().err
    assert build_css._executable("custom/tailwindcss") == build_css.Path("custom/tailwindcss")


def test_fragment_target_accepts_only_safe_ids() -> None:
    from app.web.rendering import fragment_target

    def req(**headers: str) -> Request:
        return _request(headers={"hx-request": "true"} | headers)

    assert fragment_target(req(**{"hx-target": "delivery-panel"})) == "delivery-panel"
    assert fragment_target(req(**{"hx-target": "x\" onload=\"1"})) is None
    assert fragment_target(req(**{"hx-target": "a", "hx-boosted": "true"})) is None
    assert fragment_target(_request(headers={"hx-target": "a"})) is None
