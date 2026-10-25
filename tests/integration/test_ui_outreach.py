"""Web interface: campaigns, draft generation, approval, delivery, recovery.

Fake providers only: no email is ever sent. The real-delivery cases use the
fake provider with ``delivers_email=True`` to stand in for an SMTP transport.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.models.campaign import Campaign
from app.models.draft_approval import DraftApproval
from app.models.email_send_attempt import EmailSendAttempt
from app.models.outreach_draft import OutreachDraft
from app.schemas.campaigns import CampaignCreate
from app.services.campaigns import create_campaign
from app.services.drafts import generate_drafts
from app.services.providers.drafts import DraftGeneratorConfigurationError
from app.services.providers.drafts_fake import FakeDraftGenerator
from app.services.providers.email import EmailDeliveryUncertainError, EmailRejectedError
from app.services.providers.email_fake import FakeEmailProvider
from tests.integration.outreach_helpers import make_saved_prospect

RECIPIENT = "ops@acme.com"
SENDER = "alex@routewise.example"
BRIEF: dict[str, str] = {
    "name": "Q4 fleet operators",
    "description": "",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
    "tone": "professional",
    "sender_name": "Alex",
    "sender_company": "RouteWise",
}
LIVE = {"email_dry_run": False, "email_sender": SENDER}
DELIVER = {"mode": "deliver", "confirm_send": "yes"}


def _count(factory: sessionmaker[Session], model: Any, **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for name, value in where.items():
        stmt = stmt.where(getattr(model, name) == value)
    with factory() as session:
        return session.execute(stmt).scalar_one()


def _draft(factory: sessionmaker[Session], draft_id: int) -> OutreachDraft:
    with factory() as session:
        draft = session.get(OutreachDraft, draft_id)
        assert draft is not None
        return draft


@pytest.fixture
def draft_id(db_session: Session, db_session_factory: sessionmaker[Session]) -> int:
    prospect = make_saved_prospect(db_session)
    brief = {k: v for k, v in BRIEF.items() if v}
    campaign = create_campaign(
        db_session, CampaignCreate.model_validate(brief | {"prospect_ids": [prospect.id]})
    )
    return generate_drafts(db_session_factory, FakeDraftGenerator(), campaign.id).items[0].draft.id


async def _approve(ui: Any, draft_id: int, **fields: str) -> Any:
    form = {"recipient_email": RECIPIENT, "revision": "1", "confirm_reviewed": "yes"} | fields
    return await ui.post(f"/ui/drafts/{draft_id}/approve", form)


# -- campaigns -------------------------------------------------------------------------
async def test_campaign_creation_validates_and_preserves_input(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    prospect = make_saved_prospect(db_session)
    async with ui_factory() as ui:
        form = await ui.get("/ui/campaigns/new")
        invalid = await ui.post(
            "/ui/campaigns",
            BRIEF
            | {"product": "mail me at sales@routewise.example", "prospect_ids": [str(prospect.id)]},
        )
        none_selected = await ui.post("/ui/campaigns", BRIEF)
        unknown = await ui.post("/ui/campaigns", BRIEF | {"prospect_ids": ["9999"]})
        created = await ui.post("/ui/campaigns", BRIEF | {"prospect_ids": [str(prospect.id)]})
        detail = await ui.get(created.headers["location"])

    assert "Acme Logistics" in form.text and 'name="prospect_ids"' in form.text
    assert invalid.status_code == 422
    assert "must not contain email addresses" in invalid.text
    assert 'value="Q4 fleet operators"' in invalid.text  # input kept
    assert f'value="{prospect.id}" class="checkbox" checked' in invalid.text
    assert none_selected.status_code == 422
    assert unknown.status_code == 422 and "9999" in unknown.text
    assert created.status_code == 303
    assert created.headers["location"].endswith("?notice=campaign_created")
    assert "Campaign created." in detail.text
    assert "No draft yet" in detail.text
    assert _count(db_session_factory, Campaign) == 1


async def test_draft_generation_is_explicit_and_drafts_stay_pending(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    prospect = make_saved_prospect(db_session)
    generator = FakeDraftGenerator()
    async with ui_factory(generator=generator) as ui:
        created = await ui.post("/ui/campaigns", BRIEF | {"prospect_ids": [str(prospect.id)]})
        campaign_path = created.headers["location"].split("?")[0]
        for _ in range(3):
            await ui.get(campaign_path)  # viewing never generates
        assert generator.prompts == []
        generated = await ui.post(f"{campaign_path}/drafts")
        page = await ui.get(generated.headers["location"])
        again = await ui.post(f"{campaign_path}/drafts")

    assert generated.headers["location"].endswith("notice=drafts_generated")
    assert "Draft generation finished." in page.text
    assert "Awaiting approval" in page.text
    assert len(generator.prompts) == 1  # the second request reused the existing draft
    assert again.status_code == 303
    assert _count(db_session_factory, OutreachDraft) == 1
    assert _count(db_session_factory, OutreachDraft, state="pending") == 1
    assert _count(db_session_factory, DraftApproval) == 0


async def test_generation_errors_are_reported(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    prospect = make_saved_prospect(db_session)
    failing = FakeDraftGenerator(
        error=DraftGeneratorConfigurationError("MISTRAL_API_KEY is not set.")
    )
    async with ui_factory(generator=failing) as ui:
        created = await ui.post("/ui/campaigns", BRIEF | {"prospect_ids": [str(prospect.id)]})
        path = created.headers["location"].split("?")[0]
        failed = await ui.post(f"{path}/drafts")

    assert failed.status_code == 503
    assert "Draft generation did not finish" in failed.text
    assert "MISTRAL_API_KEY is not set." in failed.text
    assert "Error code: provider_not_configured." in failed.text
    assert _count(db_session_factory, OutreachDraft) == 0


# -- draft review and approval -------------------------------------------------------------
async def test_review_page_shows_exact_message_and_provenance(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    draft = _draft(db_session_factory, draft_id)
    async with ui_factory() as ui:
        page = await ui.get(f"/ui/drafts/{draft_id}")
        queue = await ui.get("/ui/drafts")

    html = page.text
    assert page.status_code == 200
    assert draft.subject in html
    assert "Not set yet: you enter the recipient when approving." in html
    assert "Awaiting approval" in html
    assert draft.prompt_version in html and draft.prompt_sha256 in html
    assert "Approve this draft" in html
    assert "Approve the draft first." in html
    assert 'name="mode"' not in html  # no send controls before approval
    assert "Dry-run mode: real email is disabled" in html
    assert draft.subject in queue.text


async def test_approval_requires_explicit_confirmation_and_valid_recipient(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    async with ui_factory() as ui:
        unconfirmed = await _approve(ui, draft_id, confirm_reviewed="")
        bad_email = await _approve(ui, draft_id, recipient_email="not-an-email")
        stale = await _approve(ui, draft_id, revision="2")

    assert unconfirmed.status_code == 422
    assert "Confirm that you reviewed the recipient" in unconfirmed.text
    assert bad_email.status_code == 422
    assert 'value="not-an-email"' in bad_email.text
    assert stale.status_code == 409
    assert _count(db_session_factory, DraftApproval) == 0
    assert _draft(db_session_factory, draft_id).state == "pending"


async def test_approval_is_recorded_and_bound_to_the_recipient(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with ui_factory(mailer=provider, **LIVE) as ui:
        approved = await _approve(ui, draft_id, recipient_email="  ops@ACME.com ")
        page = await ui.get(approved.headers["location"])
        repeat = await _approve(ui, draft_id, recipient_email="ops@acme.com")
        other = await _approve(ui, draft_id, recipient_email="ceo@acme.com")

    assert approved.status_code == 303
    assert approved.headers["location"].endswith("notice=approved")
    assert "Nothing has been sent." in page.text
    assert "Approved, not sent" in page.text
    assert "<strong>ops@acme.com</strong>" in page.text
    assert "Matches this exact content" in page.text
    assert "local-operator" in page.text and "not a verified identity" in page.text
    assert repeat.headers["location"].endswith("notice=approval_exists")
    assert other.status_code == 409 and "That action was not completed" in other.text
    with db_session_factory() as session:
        approval = session.execute(select(DraftApproval)).scalar_one()
    assert approval.recipient_email == RECIPIENT
    # Approval never sends.
    assert provider.attempts == 0
    assert _count(db_session_factory, EmailSendAttempt) == 0


async def test_forms_cannot_change_recipient_subject_or_body(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    original = _draft(db_session_factory, draft_id)
    async with ui_factory(mailer=provider, **LIVE) as ui:
        smuggled_approval = await _approve(
            ui, draft_id, subject="Changed", body="Changed body"
        )
        assert _count(db_session_factory, DraftApproval) == 0
        await _approve(ui, draft_id)
        smuggled_send = await ui.post(
            f"/ui/drafts/{draft_id}/send",
            {"mode": "deliver", "confirm_send": "yes", "recipient_email": "attacker@evil.example"},
        )
        smuggled_body = await ui.post(
            f"/ui/drafts/{draft_id}/send",
            {"mode": "deliver", "confirm_send": "yes", "body": "Changed"},
        )
        sent = await ui.post(f"/ui/drafts/{draft_id}/send", DELIVER)

    assert smuggled_approval.status_code == 422
    assert "unexpected fields: body, subject" in smuggled_approval.text
    assert smuggled_send.status_code == 422 and "recipient_email" in smuggled_send.text
    assert smuggled_body.status_code == 422
    assert sent.headers["location"].endswith("notice=sent")
    assert len(provider.sent) == 1
    message = provider.sent[0]
    assert message.recipient == RECIPIENT
    assert message.subject == original.subject
    assert message.body == original.body
    after = _draft(db_session_factory, draft_id)
    assert (after.subject, after.body) == (original.subject, original.body)


# -- sending -------------------------------------------------------------------------------
async def test_sending_an_unapproved_draft_is_refused(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with ui_factory(mailer=provider, **LIVE) as ui:
        dry = await ui.post(f"/ui/drafts/{draft_id}/send", {"mode": "dry_run"})
        real = await ui.post(f"/ui/drafts/{draft_id}/send", DELIVER)

    for response in (dry, real):
        assert response.status_code == 409
        assert "has not been approved" in response.text
    assert provider.attempts == 0
    assert _count(db_session_factory, EmailSendAttempt) == 0


async def test_dry_run_mode_never_delivers(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with ui_factory(mailer=provider, email_sender=SENDER) as ui:
        await _approve(ui, draft_id)
        page = await ui.get(f"/ui/drafts/{draft_id}")
        dry = await ui.post(f"/ui/drafts/{draft_id}/send", {"mode": "dry_run"})
        after = await ui.get(dry.headers["location"])
        forced = await ui.post(
            f"/ui/drafts/{draft_id}/send", DELIVER
        )
        history = await ui.get("/ui/deliveries")

    assert "Dry-run mode: real email is disabled" in page.text
    assert "EMAIL_DRY_RUN is true" in page.text
    assert "Record a dry run" in page.text
    assert 'value="deliver"' not in page.text
    assert dry.headers["location"].endswith("notice=dry_run")
    assert "Dry run recorded. No email was sent" in after.text
    assert "Dry run (simulated)" in after.text
    assert "Approved, not sent" in after.text
    assert forced.status_code == 409 and "EMAIL_DRY_RUN=true" in forced.text
    assert provider.attempts == 0
    assert "Dry run (simulated)" in history.text
    assert RECIPIENT not in history.text  # recipients only on the draft page


async def test_fake_provider_cannot_really_deliver_even_with_dry_run_off(
    ui_factory: Any, draft_id: int
) -> None:
    async with ui_factory(**LIVE) as ui:  # the configured fake: delivers_email=False
        await _approve(ui, draft_id)
        page = await ui.get(f"/ui/drafts/{draft_id}")
        forced = await ui.post(
            f"/ui/drafts/{draft_id}/send", DELIVER
        )
    assert "only simulates delivery" in page.text
    assert 'value="deliver"' not in page.text
    assert forced.status_code == 409


async def test_real_delivery_needs_confirmation_and_happens_once(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with ui_factory(mailer=provider, **LIVE) as ui:
        await _approve(ui, draft_id)
        ready = await ui.get(f"/ui/drafts/{draft_id}")
        unconfirmed = await ui.post(f"/ui/drafts/{draft_id}/send", {"mode": "deliver"})
        sent = await ui.post(f"/ui/drafts/{draft_id}/send", DELIVER)
        page = await ui.get(sent.headers["location"])
        repeat = await ui.post(
            f"/ui/drafts/{draft_id}/send", DELIVER
        )

    assert "Email: real delivery enabled" in ready.text
    assert "Real delivery is enabled" in ready.text
    assert f"Send one real email to {RECIPIENT}" in ready.text
    assert unconfirmed.status_code == 422
    assert "Confirm that you want to send this one email now." in unconfirmed.text
    assert sent.headers["location"].endswith("notice=sent")
    assert "The mail server accepted this message." in page.text
    assert "Real delivery" in page.text and "Message-ID" in page.text
    assert 'value="deliver"' not in page.text
    assert repeat.headers["location"].endswith("notice=already_sent")
    assert len(provider.sent) == 1
    assert _draft(db_session_factory, draft_id).state == "sent"


async def test_definite_failure_is_explained_and_retry_safe(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True, error=EmailRejectedError("550 no such user"))
    async with ui_factory(mailer=provider, **LIVE) as ui:
        await _approve(ui, draft_id)
        failed = await ui.post(f"/ui/drafts/{draft_id}/send", DELIVER)

    assert failed.status_code == 502
    assert "The mail server refused the message. Nothing was delivered." in failed.text
    assert "you may try again" in failed.text
    assert _draft(db_session_factory, draft_id).state == "approved"
    assert provider.attempts == 1


async def test_uncertain_delivery_blocks_sending_until_resolved(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(
        delivers_email=True, error=EmailDeliveryUncertainError("connection lost after DATA")
    )
    async with ui_factory(mailer=provider, **LIVE) as ui:
        await _approve(ui, draft_id)
        uncertain = await ui.post(
            f"/ui/drafts/{draft_id}/send", DELIVER
        )
        page = await ui.get(uncertain.headers["location"])
        attention = await ui.get("/ui/drafts?view=attention")
        retry = await ui.post(
            f"/ui/drafts/{draft_id}/send", DELIVER
        )
        unconfirmed = await ui.post(f"/ui/drafts/{draft_id}/resolve", {"delivered": "no"})
        resolved = await ui.post(
            f"/ui/drafts/{draft_id}/resolve",
            {"delivered": "no", "note": "Not in Sent folder", "confirm_checked": "yes"},
        )
        after = await ui.get(resolved.headers["location"])

    assert uncertain.headers["location"].endswith("notice=delivery_uncertain")
    html = page.text
    assert "Delivery uncertain: the email may already have been sent" in html
    assert "Do not send it again until you have checked." in html
    assert "Explee never retries automatically." in html
    assert "Message-ID" in html and "explee.draft-" in html
    assert 'value="deliver"' not in html
    assert "Record verified outcome" in html
    assert 'class="nav-count"' in html  # attention badge in the navigation
    assert "These drafts need a person." in attention.text
    assert retry.status_code == 409  # backend refuses while uncertain
    assert provider.attempts == 1
    assert unconfirmed.status_code == 422
    assert "Confirm that you checked the mailbox" in unconfirmed.text
    assert resolved.headers["location"].endswith("notice=resolved_not_delivered")
    assert "Manual resolution" in after.text and "Not in Sent folder" in after.text
    assert "An operator confirmed the message was not delivered." in after.text
    assert _draft(db_session_factory, draft_id).state == "approved"


async def test_resolving_as_delivered_marks_sent(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True, error=EmailDeliveryUncertainError("?"))
    async with ui_factory(mailer=provider, **LIVE) as ui:
        await _approve(ui, draft_id)
        await ui.post(f"/ui/drafts/{draft_id}/send", DELIVER)
        resolved = await ui.post(
            f"/ui/drafts/{draft_id}/resolve", {"delivered": "yes", "confirm_checked": "yes"}
        )
        repeat = await ui.post(
            f"/ui/drafts/{draft_id}/resolve", {"delivered": "yes", "confirm_checked": "yes"}
        )

    assert resolved.headers["location"].endswith("notice=resolved_delivered")
    assert repeat.status_code == 409  # nothing left to resolve
    assert _draft(db_session_factory, draft_id).state == "sent"


async def test_in_progress_send_is_shown_and_polled_read_only(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session], db_session: Session
) -> None:
    async with ui_factory() as ui:
        await _approve(ui, draft_id)
    approval = db_session.execute(select(DraftApproval)).scalar_one()
    db_session.add(
        EmailSendAttempt(
            draft_id=draft_id,
            approval_id=approval.id,
            mode="real",
            provider="smtp",
            outcome="in_progress",
            started_at=datetime.now(UTC),
        )
    )
    db_session.execute(
        update(OutreachDraft).where(OutreachDraft.id == draft_id).values(state="sending")
    )
    db_session.commit()

    async with ui_factory() as ui:
        page = await ui.get(f"/ui/drafts/{draft_id}")
        panel = await ui.get(
            f"/ui/drafts/{draft_id}/delivery",
            headers={"HX-Request": "true", "HX-Target": "delivery-panel"},
        )
        plain = await ui.get(f"/ui/drafts/{draft_id}/delivery")
        early = await ui.post(
            f"/ui/drafts/{draft_id}/resolve", {"delivered": "no", "confirm_checked": "yes"}
        )

    assert "A delivery attempt is in progress." in page.text
    assert 'hx-trigger="every 5s"' in page.text
    assert "Record verified outcome" not in page.text
    assert panel.text.lstrip().startswith('<section id="delivery-panel"')
    assert plain.status_code == 303
    assert early.status_code == 409  # the backend refuses a recent claim
    assert _draft(db_session_factory, draft_id).state == "sending"


async def test_page_loads_never_approve_or_send(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]
) -> None:
    provider = FakeEmailProvider(delivers_email=True)
    async with ui_factory(mailer=provider, **LIVE) as ui:
        for path in (
            f"/ui/drafts/{draft_id}",
            f"/ui/drafts/{draft_id}?notice=sent",
            "/ui/drafts?view=all",
            "/ui/deliveries",
            f"/ui/drafts/{draft_id}/send",
            f"/ui/drafts/{draft_id}/approve",
        ):
            await ui.get(path)
    assert provider.attempts == 0
    assert _count(db_session_factory, DraftApproval) == 0
    assert _count(db_session_factory, EmailSendAttempt) == 0
    assert _draft(db_session_factory, draft_id).state == "pending"


async def test_delivery_history_filters_and_fragment(ui_factory: Any, draft_id: int) -> None:
    async with ui_factory(email_sender=SENDER) as ui:
        await _approve(ui, draft_id)
        await ui.post(f"/ui/drafts/{draft_id}/send", {"mode": "dry_run"})
        everything = await ui.get("/ui/deliveries")
        filtered = await ui.get("/ui/deliveries", params={"outcome": "sent"})
        bogus = await ui.get("/ui/deliveries", params={"outcome": "'; drop table"})
        fragment = await ui.get(
            "/ui/deliveries",
            params={"mode": "dry_run"},
            headers={"HX-Request": "true", "HX-Target": "delivery-results"},
        )

    assert f"#{draft_id} · rev 1" in everything.text
    assert "What the outcomes mean" in everything.text
    assert "No delivery attempts" in filtered.text
    assert bogus.status_code == 200 and f"#{draft_id} · rev 1" in bogus.text
    assert fragment.text.lstrip().startswith('<section id="delivery-results"')


# -- PR #11 review follow-ups ---------------------------------------------------------------
async def test_campaign_page_shows_every_prospect_beyond_100_drafts(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    prospects = [
        make_saved_prospect(db_session, name=f"Prospect {i:02d}", domain=f"p{i:02d}.com")
        for i in range(35)
    ]
    brief = {k: v for k, v in BRIEF.items() if v}
    campaign = create_campaign(
        db_session,
        CampaignCreate.model_validate(brief | {"prospect_ids": [p.id for p in prospects]}),
    )
    while generate_drafts(db_session_factory, FakeDraftGenerator(), campaign.id).remaining:
        pass
    # Two more revisions per prospect: 105 drafts, more than any single page.
    for revision in (2, 3):
        db_session.execute(
            text(
                "INSERT INTO outreach_drafts (campaign_id, saved_prospect_id, revision, subject, "
                "body, prompt_version, prompt_sha256, provider, model, generation_metadata, "
                "provenance, generated_at) SELECT campaign_id, saved_prospect_id, :rev, "
                "'Revision ' || :rev, body, prompt_version, prompt_sha256, provider, model, "
                "generation_metadata, provenance, generated_at FROM outreach_drafts "
                "WHERE campaign_id = :c AND revision = 1"
            ),
            {"rev": revision, "c": campaign.id},
        )
    db_session.commit()

    async with ui_factory() as ui:
        page = await ui.get(f"/ui/campaigns/{campaign.id}")

    html = page.text
    assert "No draft yet" not in html
    assert "0 prospects without a draft." in html
    assert "105 drafts in total" in html
    assert html.count("Revision 3") == 35  # every row links its newest revision


async def test_fragment_errors_replace_only_their_target(
    ui_factory: Any, draft_id: int
) -> None:
    async with ui_factory() as ui:
        missing = await ui.get(
            "/ui/drafts/9999/delivery",
            headers={"HX-Request": "true", "HX-Target": "delivery-panel"},
        )
        full_page = await ui.get("/ui/drafts/9999")
    assert missing.status_code == 404
    assert missing.text.lstrip().startswith('<section id="delivery-panel"')
    assert "<html" not in missing.text and "Draft 9999" in missing.text
    assert "<html" in full_page.text


async def test_fragments_skip_the_attention_count(
    ui_factory: Any, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web import rendering

    calls: list[str] = []
    original = rendering._attention_count

    def counting(request: Any) -> int | None:
        calls.append(request.url.path)
        return original(request)

    monkeypatch.setattr(rendering, "_attention_count", counting)
    async with ui_factory() as ui:
        await ui.get(
            f"/ui/drafts/{draft_id}/delivery",
            headers={"HX-Request": "true", "HX-Target": "delivery-panel"},
        )
        await ui.get(
            "/ui/deliveries", headers={"HX-Request": "true", "HX-Target": "delivery-results"}
        )
        assert calls == []
        await ui.get(f"/ui/drafts/{draft_id}")
    assert calls == [f"/ui/drafts/{draft_id}"]
