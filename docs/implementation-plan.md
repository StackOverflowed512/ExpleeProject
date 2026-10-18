# Implementation Plan

Phases are sequential. Each phase ends with its exit criteria met, tests
passing, and this document updated. No phase starts until the previous one
is accepted.

## P0 — Foundation (this phase)

**Scope:** project layout, typed settings, FastAPI factory, health endpoint,
error envelope, SQLAlchemy engine/session wiring, Alembic configuration,
test harness, setup documentation.

**Exit criteria**

- [x] Coherent `app/`, `tests/`, `docs/`, `scripts/`, `alembic/` layout.
- [x] Dependencies install into a local virtual environment — verified with
      Python 3.14.8, `pip install -e ".[dev]"` on Windows 11.
- [x] `uvicorn app.main:app` starts; `GET /health` returns the documented JSON
      (verified 2026-10-18; `/docs` and `/openapi.json` also 200).
- [x] `pytest` passes: 36 passed, no database, network, or Mistral credentials.
- [x] Alembic reads `DATABASE_URL` from settings; `alembic check` reports no
      pending operations.
- [x] `.gitignore` excludes `.env`, virtual environments, and caches.
- [x] `README.md`, `CLAUDE.md`, and docs are ready for future sessions.
- [x] PostgreSQL connectivity verified — PostgreSQL 17.5 via `scripts.check_db`.

**Deferred:** ORM models and the first migration arrive with the features
that need them; Phase 0 creates no empty feature modules.

## P1 — Explorer: discovery

**Scope:** `SearchProvider` protocol and one real adapter; company candidate
model and first migration; discovery endpoint/service; provider selection
through settings.

**Exit criteria**

- Provider adapter is replaceable and injectable; tests use a fake adapter.
- Discovery results are normalized and persisted with their source.
- No provider data is invented; missing fields stay missing.
- Unit and integration tests cover normalization, deduplication, and errors.
- First Alembic migration exists and upgrades from an empty database.

## P2 — Explorer: research and enrichment

**Scope:** website fetching/extraction protocol; research records with source
URLs; enrichment service combining discovery and research; failure and
partial-data handling.

**Exit criteria**

- Extraction results store their provenance (URL, fetched timestamp).
- Fetch failures degrade gracefully and are visible in logs.
- Enrichment is idempotent for the same prospect and input.
- Tests use recorded fixtures, never live websites.

## P3 — Explorer: qualification and saved prospects

**Scope:** rule-based qualification scoring with rationale; saved prospect
model and API; search/filtering of saved prospects; deduplication rules.

**Exit criteria**

- Qualification thresholds and rationale are persisted and auditable.
- Saving a prospect requires qualification output.
- Duplicate prospects are prevented or merged deterministically.
- End-to-end flow tested with fakes from discovery to saved prospect.

## P4 — Outreach: campaigns and drafts

**Scope:** campaign model built from saved prospects; Mistral adapter behind
an `AIProvider` protocol; draft generation with versioned prompts; draft
states.

**Exit criteria**

- Mistral is reachable only through the adapter; tests use a fake provider.
- Drafts persist prompt version, model, and raw response metadata.
- Generation failures never produce a partially saved draft.
- No credentials are required to run the suite.

## P5 — Outreach: approval and delivery

**Scope:** explicit approval transition enforced by the backend; email
provider protocol and one real adapter; send audit trail; dry-run path.

**Exit criteria**

- Sending requires a recorded human approval; the service rejects anything
  else, regardless of caller.
- Sends are idempotent per draft and fully audited (attempt, outcome, time).
- Failure handling and retry policy documented and tested.
- No email is sent in tests; the real adapter is only exercised manually.

## P6 — Web interface

**Scope:** Jinja2 templates and HTMX interactions for Explorer and Outreach;
server-rendered pages calling existing services.

**Exit criteria**

- Routes remain thin; templates contain no business rules.
- Approval UI calls the backend approval endpoint; UI cannot bypass it.
- Accessibility basics (labels, focus states) and error rendering covered.

## P7 — Reliability and operations

**Scope:** structured request logging, provider timeouts/retries, rate-limit
handling, readiness checks, backup/restore notes.

**Exit criteria**

- Timeouts and bounded retries configured for every provider.
- Readiness endpoint reports database state without breaking liveness.
- Operational runbook added to `docs/`.

## P8 — Hardening and release

**Scope:** full end-to-end pass with real providers, security review of
secret handling and approval gate, performance sanity checks, final docs.

**Exit criteria**

- End-to-end Explorer → Outreach run completes with real providers and a
  reviewed send.
- Secret scanning clean; no credentials in Git history.
- Documentation reflects the shipped system.
