"""Integration tests for approval and delivery services (PostgreSQL).

Requires ``TEST_DATABASE_URL``; skipped otherwise. Delivery always uses
:class:`FakeEmailProvider`, a scripted provider, or an SMTP adapter whose
network access is blocked; no email is ever sent.
"""

from __future__ import annotations

import smtplib
import socket
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models.draft_approval import DraftApproval
from app.models.email_send_attempt import EmailSendAttempt
from app.models.outreach_draft import OutreachDraft
from app.schemas.campaigns import CampaignCreate
from app.services import delivery as delivery_service
from app.services.approvals import (
    ApprovalResult,
    DraftAlreadyApprovedError,
    DraftNotApprovableError,
    DraftRevisionMismatchError,
    approve_draft,
    content_sha256,
)
from app.services.campaigns import create_campaign
from app.services.delivery import (
    ApprovalContentMismatchError,
    DeliveryDisabledError,
    DeliveryFailed,
    DeliveryInProgressError,
    DeliveryNotResolvableError,
    DeliveryOutcomeUncertainError,
    DeliveryPersistenceError,
    DraftNotApprovedError,
    ProspectAlreadyEmailedError,
    SendResult,
    resolve_delivery,
    send_draft,
)
from app.services.drafts import DraftNotFoundError, generate_drafts
from app.services.providers.drafts_fake import FakeDraftGenerator
from app.services.providers.email import (
    DeliveryResult,
    EmailDeliveryUncertainError,
    EmailProvider,
    EmailNotConfiguredError,
    EmailRejectedError,
    OutgoingEmail,
)
from app.services.providers.email_fake import FakeEmailProvider
from app.services.providers.email_smtp import SmtpEmailProvider
from tests.integration.outreach_helpers import make_saved_prospect

SENDER = "alex@routewise.example"
RECIPIENT = "ops@acme.com"
ACTOR = "local-operator"
TIMEOUT = 20
BRIEF: dict[str, object] = {
    "name": "Delivery",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
}


@pytest.fixture
def draft_id(db_session: Session, db_session_factory: sessionmaker[Session]) -> int:
    prospect = make_saved_prospect(db_session)
    campaign = create_campaign(
        db_session, CampaignCreate.model_validate(BRIEF | {"prospect_ids": [prospect.id]})
    )
    result = generate_drafts(db_session_factory, FakeDraftGenerator(), campaign.id)
    return result.items[0].draft.id


def _approve(
    session: Session, draft_id: int, recipient: str = RECIPIENT, revision: int = 1
) -> ApprovalResult:
    return approve_draft(session, draft_id, recipient=recipient, revision=revision, actor=ACTOR)


def _send(
    factory: sessionmaker[Session],
    provider: EmailProvider,
    draft_id: int,
    *,
    mode: str = "deliver",
    dry_run_only: bool = False,
    sender: str | None = SENDER,
) -> SendResult:
    return send_draft(
        factory, provider, draft_id, mode=mode, dry_run_only=dry_run_only, sender=sender
    )


def _state(factory: sessionmaker[Session], draft_id: int) -> str:
    with factory() as session:
        return session.get(OutreachDraft, draft_id).state  # type: ignore[union-attr]


def _attempts(factory: sessionmaker[Session], draft_id: int) -> list[EmailSendAttempt]:
    with factory() as session:
        return list(
            session.execute(
                select(EmailSendAttempt)
                .where(EmailSendAttempt.draft_id == draft_id)
                .order_by(EmailSendAttempt.id)
            ).scalars()
        )


class ExplodingProvider:
    """A provider that must never be called."""

    name = "exploding"

    def send(self, message: OutgoingEmail) -> DeliveryResult:
        raise AssertionError("provider must not be called")


# -- approval --------------------------------------------------------------------
def test_approval_binds_revision_recipient_and_content(
    db_session: Session, draft_id: int
) -> None:
    result = _approve(db_session, draft_id)

    assert result.created is True
    approval = result.approval
    draft = db_session.get(OutreachDraft, draft_id)
    assert draft is not None and draft.state == "approved"
    assert (approval.draft_id, approval.draft_revision) == (draft_id, 1)
    assert approval.recipient_email == RECIPIENT
    assert approval.approved_by == ACTOR
    assert approval.approved_at is not None
    assert approval.content_sha256 == content_sha256(
        draft_id=draft_id, revision=1, recipient=RECIPIENT, subject=draft.subject, body=draft.body
    )


def test_repeated_identical_approval_is_idempotent(db_session: Session, draft_id: int) -> None:
    first = _approve(db_session, draft_id)
    second = _approve(db_session, draft_id)
    assert second.created is False
    assert second.approval.id == first.approval.id
    count = db_session.execute(text("SELECT count(*) FROM draft_approvals")).scalar_one()
    assert count == 1


def test_approval_cannot_be_changed_to_another_recipient(
    db_session: Session, draft_id: int
) -> None:
    _approve(db_session, draft_id)
    with pytest.raises(DraftAlreadyApprovedError):
        _approve(db_session, draft_id, recipient="ceo@acme.com")
    db_session.expire_all()
    approval = db_session.execute(select(DraftApproval)).scalar_one()
    assert approval.recipient_email == RECIPIENT


def test_approval_requires_matching_revision_and_existing_draft(
    db_session: Session, draft_id: int
) -> None:
    with pytest.raises(DraftRevisionMismatchError):
        _approve(db_session, draft_id, revision=2)
    with pytest.raises(DraftNotFoundError):
        _approve(db_session, 999_999)
    assert db_session.execute(text("SELECT count(*) FROM draft_approvals")).scalar_one() == 0


def test_only_pending_drafts_can_be_approved(db_session: Session, draft_id: int) -> None:
    db_session.execute(
        update(OutreachDraft).where(OutreachDraft.id == draft_id).values(state="sent")
    )
    db_session.commit()
    with pytest.raises(DraftNotApprovableError):
        _approve(db_session, draft_id)


def test_concurrent_approvals_create_one_record(
    db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    results: list[object] = []
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def worker() -> None:
        with db_session_factory() as session:
            barrier.wait()
            results.append(_approve(session, draft_id))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
    assert sorted(r.created for r in results) == [False, True]  # type: ignore[attr-defined]
    with db_session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM draft_approvals")).scalar_one() == 1


# -- send preconditions ----------------------------------------------------------
def test_send_without_approval_is_rejected(
    db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    for mode in ("dry_run", "deliver"):
        with pytest.raises(DraftNotApprovedError):
            _send(db_session_factory, ExplodingProvider(), draft_id, mode=mode)
    assert _attempts(db_session_factory, draft_id) == []
    with pytest.raises(DraftNotFoundError):
        _send(db_session_factory, ExplodingProvider(), 424242)


def test_dry_run_records_outcome_and_never_calls_a_provider(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    result = _send(
        db_session_factory, ExplodingProvider(), draft_id, mode="dry_run", dry_run_only=True
    )
    assert result.outcome == "dry_run"
    assert result.attempt.mode == "dry_run"
    assert result.attempt.outcome == "dry_run"
    assert result.attempt.provider == "none"
    assert _state(db_session_factory, draft_id) == "approved"


def test_dry_run_opens_no_smtp_connection_even_with_smtp_configured(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    draft_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(smtplib.SMTP, "__init__", no_network)
    monkeypatch.setattr(smtplib.SMTP_SSL, "__init__", no_network)
    smtp = SmtpEmailProvider(host="smtp.example.com", port=587, security="starttls")
    _approve(db_session, draft_id)

    result = _send(db_session_factory, smtp, draft_id, mode="dry_run", dry_run_only=False)
    assert result.outcome == "dry_run"


def test_real_delivery_is_refused_while_dry_run_only(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    with pytest.raises(DeliveryDisabledError):
        _send(db_session_factory, ExplodingProvider(), draft_id, dry_run_only=True)
    assert _attempts(db_session_factory, draft_id) == []
    assert _state(db_session_factory, draft_id) == "approved"


def test_real_delivery_requires_a_sender(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    with pytest.raises(EmailNotConfiguredError):
        _send(db_session_factory, ExplodingProvider(), draft_id, sender=None)
    assert _attempts(db_session_factory, draft_id) == []


# -- successful delivery and idempotency ---------------------------------------
def test_successful_delivery_uses_the_approved_snapshot_and_is_audited(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    provider = FakeEmailProvider()

    result = _send(db_session_factory, provider, draft_id)

    assert result.outcome == "sent"
    (message,) = provider.sent
    draft = db_session.get(OutreachDraft, draft_id)
    assert draft is not None
    assert (message.sender, message.recipient) == (SENDER, RECIPIENT)
    assert (message.subject, message.body) == (draft.subject, draft.body)
    attempt = result.attempt
    assert message.message_id == attempt.message_id
    assert attempt.message_id == f"<explee.draft-{draft_id}.attempt-{attempt.id}@routewise.example>"
    assert (attempt.mode, attempt.outcome, attempt.provider) == ("real", "sent", "fake")
    assert attempt.provider_reference == "fake-1"
    assert attempt.failure_category is None
    assert attempt.finished_at is not None and attempt.duration_ms is not None
    assert _state(db_session_factory, draft_id) == "sent"


def test_sent_draft_is_never_sent_again(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    provider = FakeEmailProvider()
    first = _send(db_session_factory, provider, draft_id)
    again = _send(db_session_factory, provider, draft_id)
    dry = _send(db_session_factory, provider, draft_id, mode="dry_run")

    assert again.outcome == "already_sent" and dry.outcome == "already_sent"
    assert again.attempt.id == first.attempt.id
    assert provider.attempts == 1
    assert len(_attempts(db_session_factory, draft_id)) == 1


# -- failures ------------------------------------------------------------------------
def test_definite_failure_is_audited_and_retry_is_allowed(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    failing = FakeEmailProvider(error=EmailRejectedError("refused"))
    with pytest.raises(DeliveryFailed) as caught:
        _send(db_session_factory, failing, draft_id)
    assert caught.value.cause.uncertain is False
    assert _state(db_session_factory, draft_id) == "approved"
    (attempt,) = _attempts(db_session_factory, draft_id)
    assert (attempt.outcome, attempt.failure_category) == ("failed", "email_rejected")
    assert attempt.id == caught.value.attempt_id

    retry = _send(db_session_factory, FakeEmailProvider(), draft_id)
    assert retry.outcome == "sent"
    assert [a.outcome for a in _attempts(db_session_factory, draft_id)] == ["failed", "sent"]


@pytest.mark.parametrize(
    "error",
    [EmailDeliveryUncertainError("maybe delivered"), RuntimeError("boom")],
)
def test_uncertain_outcome_blocks_automatic_resend(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    draft_id: int,
    error: Exception,
) -> None:
    _approve(db_session, draft_id)

    class Uncertain:
        name = "flaky"

        def send(self, message: OutgoingEmail) -> DeliveryResult:
            raise error

    with pytest.raises(DeliveryFailed) as caught:
        _send(db_session_factory, Uncertain(), draft_id)
    assert caught.value.cause.uncertain is True
    assert _state(db_session_factory, draft_id) == "delivery_uncertain"
    (attempt,) = _attempts(db_session_factory, draft_id)
    assert attempt.outcome == "uncertain"
    assert attempt.failure_category == "email_delivery_uncertain"

    for mode in ("deliver", "dry_run"):
        with pytest.raises(DeliveryOutcomeUncertainError):
            _send(db_session_factory, ExplodingProvider(), draft_id, mode=mode)
    assert len(_attempts(db_session_factory, draft_id)) == 1


# -- approval integrity --------------------------------------------------------------
@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE outreach_drafts SET body = body || ' P.S. tampered'",
        "UPDATE outreach_drafts SET subject = 'Different subject'",
        "UPDATE draft_approvals SET recipient_email = 'attacker@evil.example'",
        "UPDATE draft_approvals SET draft_revision = 2",
    ],
)
def test_changed_content_or_recipient_cannot_be_sent(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    draft_id: int,
    tamper: str,
) -> None:
    _approve(db_session, draft_id)
    db_session.execute(text(tamper))
    db_session.commit()
    for mode in ("dry_run", "deliver"):
        with pytest.raises(ApprovalContentMismatchError):
            _send(db_session_factory, ExplodingProvider(), draft_id, mode=mode)
    assert _attempts(db_session_factory, draft_id) == []


def test_approval_of_another_draft_cannot_authorize_delivery(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    other_prospect = make_saved_prospect(db_session, name="Beta", domain="beta.io")
    campaign = create_campaign(
        db_session, CampaignCreate.model_validate(BRIEF | {"prospect_ids": [other_prospect.id]})
    )
    generated = generate_drafts(db_session_factory, FakeDraftGenerator(), campaign.id)
    other_id = generated.items[0].draft.id
    _approve(db_session, other_id)
    # Re-point the other draft's approval at this draft and fake its state.
    db_session.execute(
        update(DraftApproval).where(DraftApproval.draft_id == other_id).values(draft_id=draft_id)
    )
    db_session.execute(
        update(OutreachDraft).where(OutreachDraft.id == draft_id).values(state="approved")
    )
    db_session.commit()
    with pytest.raises(ApprovalContentMismatchError):
        _send(db_session_factory, ExplodingProvider(), draft_id)


def test_only_one_revision_per_campaign_prospect_can_be_emailed(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    campaign_id = db_session.get(OutreachDraft, draft_id).campaign_id  # type: ignore[union-attr]
    second = generate_drafts(
        db_session_factory, FakeDraftGenerator(), campaign_id, regenerate=True
    ).items[0].draft
    _approve(db_session, draft_id)
    _approve(db_session, second.id, revision=2)
    _send(db_session_factory, FakeEmailProvider(), draft_id)

    provider = FakeEmailProvider()
    with pytest.raises(ProspectAlreadyEmailedError):
        _send(db_session_factory, provider, second.id)
    assert provider.attempts == 0
    assert _state(db_session_factory, second.id) == "approved"


# -- manual resolution -------------------------------------------------------------
def _make_uncertain(session: Session, factory: sessionmaker[Session], draft_id: int) -> None:
    _approve(session, draft_id)
    with pytest.raises(DeliveryFailed):
        _send(factory, FakeEmailProvider(error=EmailDeliveryUncertainError("?")), draft_id)


def test_resolve_as_delivered_marks_sent(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _make_uncertain(db_session, db_session_factory, draft_id)
    with db_session_factory() as session:
        resolution = resolve_delivery(session, draft_id, delivered=True, note="Seen in Sent folder")
    assert (resolution.mode, resolution.outcome) == ("manual_resolution", "sent")
    assert resolution.note == "Seen in Sent folder"
    assert _state(db_session_factory, draft_id) == "sent"
    again = _send(db_session_factory, ExplodingProvider(), draft_id)
    assert again.outcome == "already_sent"


def test_resolve_as_not_delivered_allows_a_new_attempt(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _make_uncertain(db_session, db_session_factory, draft_id)
    with db_session_factory() as session:
        resolution = resolve_delivery(session, draft_id, delivered=False, note=None)
    assert (resolution.outcome, resolution.failure_category) == (
        "failed",
        "confirmed_not_delivered",
    )
    assert _state(db_session_factory, draft_id) == "approved"
    assert _send(db_session_factory, FakeEmailProvider(), draft_id).outcome == "sent"
    outcomes = [(a.mode, a.outcome) for a in _attempts(db_session_factory, draft_id)]
    assert outcomes == [("real", "uncertain"), ("manual_resolution", "failed"), ("real", "sent")]


def test_resolution_is_refused_for_normal_states(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    with db_session_factory() as session:
        with pytest.raises(DeliveryNotResolvableError):
            resolve_delivery(session, draft_id, delivered=True, note=None)
    _approve(db_session, draft_id)
    with db_session_factory() as session:
        with pytest.raises(DeliveryNotResolvableError):
            resolve_delivery(session, draft_id, delivered=True, note=None)


def test_interrupted_send_is_resolvable_only_once_stale(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    """Simulate a process that died after claiming (draft stuck in 'sending')."""
    approval = _approve(db_session, draft_id).approval
    started = datetime.now(UTC)
    db_session.add(
        EmailSendAttempt(
            draft_id=draft_id,
            approval_id=approval.id,
            mode="real",
            provider="smtp",
            outcome="in_progress",
            started_at=started,
        )
    )
    db_session.execute(
        update(OutreachDraft).where(OutreachDraft.id == draft_id).values(state="sending")
    )
    db_session.commit()

    with pytest.raises(DeliveryInProgressError):
        _send(db_session_factory, ExplodingProvider(), draft_id)
    with db_session_factory() as session:
        with pytest.raises(DeliveryInProgressError):
            resolve_delivery(session, draft_id, delivered=False, note=None, now=started)
    with db_session_factory() as session:
        resolve_delivery(
            session,
            draft_id,
            delivered=False,
            note="checked logs",
            now=started + timedelta(hours=1),
        )
    outcomes = [(a.outcome, a.failure_category) for a in _attempts(db_session_factory, draft_id)]
    assert outcomes == [("uncertain", "interrupted"), ("failed", "confirmed_not_delivered")]
    assert _state(db_session_factory, draft_id) == "approved"


# -- persistence and concurrency ------------------------------------------------------
def test_claim_failure_rolls_back_and_sends_nothing(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    draft_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approve(db_session, draft_id)

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("INSERT", {}, Exception("disk full"))

    monkeypatch.setattr(delivery_service, "_verify_and_claim", broken)
    provider = FakeEmailProvider()
    with pytest.raises(DeliveryPersistenceError):
        _send(db_session_factory, provider, draft_id)
    monkeypatch.undo()
    assert provider.attempts == 0
    assert _state(db_session_factory, draft_id) == "approved"
    assert _attempts(db_session_factory, draft_id) == []


class BlockingProvider:
    """Holds delivery open until released, to create a real overlap."""

    name = "blocking"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def send(self, message: OutgoingEmail) -> DeliveryResult:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(TIMEOUT)
        return DeliveryResult(provider=self.name, provider_reference="ok")


def _send_in_thread(
    factory: sessionmaker[Session], provider: EmailProvider, draft_id: int
) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = _send(factory, provider, draft_id)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_concurrent_sends_of_one_draft_deliver_once(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    _approve(db_session, draft_id)
    provider = BlockingProvider()
    thread, outcome = _send_in_thread(db_session_factory, provider, draft_id)
    assert provider.entered.wait(TIMEOUT)

    with pytest.raises(DeliveryInProgressError):
        _send(db_session_factory, provider, draft_id)

    provider.release.set()
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert outcome["result"].outcome == "sent"  # type: ignore[attr-defined]
    assert provider.calls == 1
    assert [a.outcome for a in _attempts(db_session_factory, draft_id)] == ["sent"]


def test_concurrent_sends_of_two_revisions_email_the_prospect_once(
    db_session: Session, db_session_factory: sessionmaker[Session], draft_id: int
) -> None:
    campaign_id = db_session.get(OutreachDraft, draft_id).campaign_id  # type: ignore[union-attr]
    second = generate_drafts(
        db_session_factory, FakeDraftGenerator(), campaign_id, regenerate=True
    ).items[0].draft
    _approve(db_session, draft_id)
    _approve(db_session, second.id, revision=2)
    provider = BlockingProvider()
    thread, outcome = _send_in_thread(db_session_factory, provider, draft_id)
    assert provider.entered.wait(TIMEOUT)

    with pytest.raises(ProspectAlreadyEmailedError):
        _send(db_session_factory, provider, second.id)

    provider.release.set()
    thread.join(TIMEOUT)
    assert outcome["result"].outcome == "sent"  # type: ignore[attr-defined]
    assert provider.calls == 1
    assert _state(db_session_factory, second.id) == "approved"
