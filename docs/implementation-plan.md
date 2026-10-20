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

**Scope:** `SearchProvider` protocol and a real search adapter (Brave in P1,
replaced by Tavily in P1.1); fake provider; `company_candidates` model and
first migration; discovery service with normalization, deduplication, and
provenance-preserving persistence; `POST /discovery/search`; provider
configuration and failure mapping.

**Implementation notes (2026-10-18; provider updated in P1.1)**

- Provider contract: `app/services/providers/search.py`; adapters
  `tavily.py` and `fake.py`; selected by `SEARCH_PROVIDER` (`tavily`
  default, `fake` for credential-free runs and tests).
- Tavily adapter posts JSON to `https://api.tavily.com/search` with a Bearer
  token and reads only documented fields (`title`, `url`, `content`,
  optional `id`). It skips malformed individual results, retries only
  transient failures (429/5xx/timeouts/connection errors) with bounded
  backoff and a bounded `Retry-After`, does not retry authentication or
  other permanent 4xx failures, and never logs the key or payload.
- Persistence: `company_candidates` with partial unique indexes on
  `domain` and `(provider, provider_result_id)`; `ON CONFLICT DO NOTHING`
  + savepoint per candidate; fill-missing-only update policy.
- API: `POST /discovery/search`, body `{query, limit}` (limit 1–10,
  default 5), response `{query, provider, requested_limit, results,
  counts}`. Errors use the shared envelope.

**Exit criteria**

- [x] Provider adapter is replaceable and injectable; tests use a fake.
- [x] Discovery results are normalized and persisted in PostgreSQL.
- [x] Deduplication is deterministic. Concurrent inserts are protected by
      partial unique indexes plus `ON CONFLICT DO NOTHING` and a savepoint
      per candidate; true simultaneity is not exercised by an automated test.
- [x] Source provenance is preserved on update (first-seen values win; only
      missing fields are filled).
- [x] First Alembic migration upgrades an empty database. Verified
      2026-10-18 on PostgreSQL 17.5: `alembic upgrade head` applied
      `0001_company_candidates`, and `alembic check` reported
      "No new upgrade operations detected."
- [x] Unit and integration tests cover normal and failure paths: 116 passed
      on 2026-10-18 (Python 3.14.8, PostgreSQL 17.5, `TEST_DATABASE_URL`
      pointing at `explee_test`). The only warning is a third-party
      Starlette/httpx deprecation raised from FastAPI's `testclient.py`.
- [x] All Phase 0 tests still pass.
- [x] No Docker or prohibited infrastructure introduced.
- [x] Documentation reflects the implementation.

**Not verified**

- The Tavily adapter has not been exercised against the live API in this
  environment because no real key was available. Automated tests use HTTPX
  mock transports; the manual command in `README.md` documents how to run a
  live check with a key.
- The independent multi-agent review did not complete: the workflow
  provider failed (quota, then an unavailable approval classifier). Defects
  fixed during implementation came from manual review, not from that review.

## P1.1 — Explorer: replace Brave with Tavily

**Scope:** swap the real discovery search adapter from Brave to Tavily,
including configuration, provider selection, error mapping, tests, and
documentation. No API, schema, or migration changes.

**Implementation notes (2026-10-20)**

- Endpoint and payload follow the official Tavily Search API:
  `POST https://api.tavily.com/search`, `Authorization: Bearer <key>`, and a
  JSON body with `query`, `max_results`, `search_depth=basic`, and
  `include_*` flags disabled.
- `TAVILY_API_KEY` replaces `SEARCH_API_KEY`; `SEARCH_PROVIDER` accepts
  `tavily` (default real provider) and `fake`. The key is optional at
  startup. Discovering with Tavily selected but no key raises
  `provider_not_configured` (503) at request time; there is no silent
  fallback.
- HTTPX remains the client (no Tavily SDK). Timeouts and retry settings are
  reused. Only transient failures are retried; `Retry-After` is honored with
  a cap. Provider errors never include keys or upstream bodies.
- No Alembic revision was added: provider configuration is environment-only.

**Exit criteria**

- [x] Tavily replaces Brave as the configured real discovery provider.
- [x] The `SearchProvider` contract and `POST /discovery/search` response
      shape are unchanged.
- [x] The app starts and `/health` works without a Tavily key.
- [x] Discovery without a configured key returns a safe,
      `provider_not_configured` error.
- [x] Automated tests use mocks/fakes and require no external API access.
- [x] Existing Phase 0 and Phase 1 regression tests pass. Full suite on
      2026-10-20: 134 passed (Python 3.14.8, PostgreSQL 17.5,
      `TEST_DATABASE_URL` pointing at `explee_test`). Without a test
      database: 107 passed, 27 skipped. `alembic check` reported no new
      upgrade operations.
- [x] No migration was added and no unrelated dependency was introduced.
- [x] Documentation and `.env.example` contain no stale Brave setup.

**Not verified**

- No live Tavily request was made: no real key was available in the
  implementation environment. Mocked tests cover request construction,
  normalization, retries, and failure mapping; live behavior remains a
  manual check.

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
