"""Real concurrency tests for draft generation (PostgreSQL, two threads).

Requires ``TEST_DATABASE_URL``; skipped otherwise. Two ``generate_drafts``
calls run at the same time in separate threads, each with its own sessions.
A barrier inside the fake generator holds both requests until both have
read the database ("no draft yet" / "latest revision is N") and both have
called the provider, so they genuinely race to persist.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.outreach_draft import OutreachDraft
from app.schemas.campaigns import CampaignCreate
from app.services.campaigns import create_campaign
from app.services.drafts import GenerationResult, generate_drafts
from app.services.providers.drafts import DraftPrompt
from app.services.providers.drafts_fake import FakeDraftGenerator, default_output
from tests.integration.outreach_helpers import make_saved_prospect

TIMEOUT_SECONDS = 20

BRIEF: dict[str, object] = {
    "name": "Concurrency",
    "product": "RouteWise",
    "value_proposition": "Cuts dispatch planning time with live route suggestions.",
    "target_audience": "Operations leaders at logistics software companies",
    "call_to_action": "Would a 20-minute call next week be useful?",
}


def _campaign(session: Session) -> tuple[int, int]:
    prospect = make_saved_prospect(session)
    campaign = create_campaign(
        session, CampaignCreate.model_validate(BRIEF | {"prospect_ids": [prospect.id]})
    )
    return campaign.id, prospect.id


def _run_concurrently(
    session_factory: sessionmaker[Session],
    campaign_id: int,
    *,
    regenerate: bool,
) -> list[GenerationResult]:
    """Run two generate_drafts calls that race to persist the same prospect."""
    barrier = threading.Barrier(2, timeout=TIMEOUT_SECONDS)

    def synchronized_output(prompt: DraftPrompt) -> str:
        # Both requests have finished reading and are "calling the provider".
        barrier.wait()
        return default_output(prompt)

    results: list[GenerationResult | None] = [None, None]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            results[index] = generate_drafts(
                session_factory,
                FakeDraftGenerator(output=synchronized_output),
                campaign_id,
                regenerate=regenerate,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT_SECONDS)
    assert not any(thread.is_alive() for thread in threads), "generation deadlocked"
    assert errors == []
    assert all(result is not None for result in results)
    return [result for result in results if result is not None]


def _drafts(session_factory: sessionmaker[Session]) -> list[OutreachDraft]:
    with session_factory() as session:
        return list(
            session.execute(
                select(OutreachDraft).order_by(OutreachDraft.revision, OutreachDraft.id)
            ).scalars()
        )


def _single_item(result: GenerationResult) -> Any:
    (item,) = result.items
    return item


@pytest.fixture
def campaign(db_session: Session) -> tuple[int, int]:
    return _campaign(db_session)


def test_simultaneous_first_generation_creates_one_draft(
    db_session_factory: sessionmaker[Session], campaign: tuple[int, int]
) -> None:
    campaign_id, prospect_id = campaign

    results = _run_concurrently(db_session_factory, campaign_id, regenerate=False)

    drafts = _drafts(db_session_factory)
    assert [(d.saved_prospect_id, d.revision) for d in drafts] == [(prospect_id, 1)]
    items = [_single_item(result) for result in results]
    assert sorted(item.outcome for item in items) == ["existing", "generated"]
    # Both callers get the persisted winning draft.
    assert {item.draft.id for item in items} == {drafts[0].id}


def test_simultaneous_regeneration_creates_one_revision_each(
    db_session_factory: sessionmaker[Session], campaign: tuple[int, int]
) -> None:
    campaign_id, prospect_id = campaign
    generate_drafts(db_session_factory, FakeDraftGenerator(), campaign_id)

    results = _run_concurrently(db_session_factory, campaign_id, regenerate=True)

    drafts = _drafts(db_session_factory)
    assert [(d.saved_prospect_id, d.revision) for d in drafts] == [
        (prospect_id, 1),
        (prospect_id, 2),
        (prospect_id, 3),
    ]
    items = [_single_item(result) for result in results]
    # Each explicit regenerate request gets its own new revision.
    assert [item.outcome for item in items] == ["generated", "generated"]
    assert sorted(item.draft.revision for item in items) == [2, 3]
    assert len({item.draft.id for item in items}) == 2


def test_simultaneous_regeneration_from_scratch(
    db_session_factory: sessionmaker[Session], campaign: tuple[int, int]
) -> None:
    campaign_id, prospect_id = campaign

    results = _run_concurrently(db_session_factory, campaign_id, regenerate=True)

    drafts = _drafts(db_session_factory)
    assert [d.revision for d in drafts] == [1, 2]
    items = [_single_item(result) for result in results]
    assert [item.outcome for item in items] == ["generated", "generated"]
    assert sorted(item.draft.revision for item in items) == [1, 2]


def test_concurrent_runs_never_violate_the_unique_revision_constraint(
    db_session_factory: sessionmaker[Session], campaign: tuple[int, int]
) -> None:
    """Repeated races stay consistent: revisions are 1..n with no gaps or dupes."""
    campaign_id, _ = campaign
    for _ in range(3):
        _run_concurrently(db_session_factory, campaign_id, regenerate=True)
    revisions = [d.revision for d in _drafts(db_session_factory)]
    assert revisions == list(range(1, 7))


def test_revision_allocation_gives_up_after_bounded_attempts(
    db_session_factory: sessionmaker[Session],
    campaign: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every candidate revision keeps colliding, fail safely, save nothing."""
    from app.services import drafts as drafts_service
    from app.services.drafts import DraftPersistenceError

    campaign_id, _ = campaign
    attempts: list[int] = []

    def always_taken(*args: object, revision: int, **kwargs: object) -> None:
        attempts.append(revision)
        return None

    monkeypatch.setattr(drafts_service, "_insert_row", always_taken)
    with pytest.raises(DraftPersistenceError, match="could not be allocated"):
        generate_drafts(
            db_session_factory, FakeDraftGenerator(), campaign_id, regenerate=True
        )
    assert len(attempts) == drafts_service._MAX_REVISION_ATTEMPTS
    assert _drafts(db_session_factory) == []
