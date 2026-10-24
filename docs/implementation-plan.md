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

**Implementation notes (2026-10-24)**

- Fetcher contract: `app/services/providers/website.py` (`WebsiteFetcher`,
  `FetchResult`, outcomes, safe failure categories). Adapters
  `website_httpx.py` (real) and `website_fake.py` (tests), selected by
  `WEBSITE_FETCHER` (`httpx` default, `fake`). No credentials needed.
- Tavily is not used: research fetches `CompanyCandidate.website_url`
  directly with HTTPX and consumes no search credits.
- SSRF: syntax validation, every-address-public resolution checks, DNS
  pinning (connect to the validated IP; hostname kept for `Host` and TLS
  SNI/cert verification), manual redirect revalidation, ports 80/443 only,
  proxies ignored, no keep-alive. Residual limitations are in
  `docs/architecture.md`.
- Extraction: BeautifulSoup `html.parser` (`beautifulsoup4` added to
  `pyproject.toml`). Scripts, styles, templates, SVG, frames, form controls,
  and comments are removed; text is whitespace-normalized and capped.
- Persistence: `company_research` (migration `0002_company_research`), one
  row per attempt. History is pruned to the 5 most recent attempts plus the
  latest usable record per candidate. Discovery provenance is never
  modified.
- Session lifecycle: research opens its own short sessions from the
  session factory, and no session is held across the outbound fetch.
- Freshness: usable research younger than `RESEARCH_FRESHNESS_HOURS`
  (default 24) is reused unless `{"refresh": true}` is sent.
- API: `POST /companies/{id}/research`, `GET /companies/{id}/research`,
  `GET /companies/{id}/enrichment`. Statuses: `available`, `partial`,
  `unavailable`, `not_researched`. Errors: `candidate_not_found` (404),
  `candidate_website_missing` (409), `validation_error` (422),
  `research_persistence_failed` (500).
- HTTPX request logging (which contains full URLs) is raised to WARNING in
  `app/logging_config.py`; research logs only candidate ID, host, and
  outcome.

**Exit criteria**

- [x] Extraction results store their provenance: `requested_url`,
      `final_url`, `fetched_at`, and `fetcher` on every attempt.
- [x] Fetch failures degrade gracefully. They are recorded as `failed` with
      a safe category, never erase earlier usable research, and are logged
      at WARNING with candidate ID and host.
- [x] Enrichment is idempotent: a pure projection of stored rows with no
      fetches or writes, covered by unit and DB tests.
- [x] Tests use a recorded HTML fixture (`tests/fixtures/websites/`), HTTPX
      mock transports, injected resolvers, and the fake fetcher. No test
      contacts a live website or DNS server.
- [x] Migration `0002_company_research` upgrades a Phase 1 database with
      data. Verified 2026-10-24 on PostgreSQL 17: the automated test
      upgrades `explee_test` from `0001` with a candidate row, then
      downgrades and re-upgrades. `alembic upgrade head` applied
      `0001 -> 0002` to the configured `explee` database, and
      `alembic check` reported "No new upgrade operations detected."
- [x] Full suite on 2026-10-24 (Python 3.14.8, PostgreSQL 17,
      `TEST_DATABASE_URL` → `explee_test`): 411 passed (after review
      follow-ups). Without a test database: 336 passed, 75 skipped. The only warning is the existing
      third-party Starlette/httpx deprecation from FastAPI's
      `testclient.py`.
- [x] Phase 0, Phase 1, and Phase 1.1 regression tests pass unchanged,
      apart from fixture updates for the new table: truncate/drop
      `company_research`, and the head revision is now `0002`.
- [x] No Tavily calls, Mistral, qualification, outreach, UI, Docker, or
      background workers added.

**Manual checks (not automated)**

- One-off live fetches with the real adapter on 2026-10-24:
  `https://example.com/`, `http://example.com`, and
  `https://www.python.org/` succeeded through the pinned-IP path. Wrong-host,
  self-signed, and expired certificates (`*.badssl.com`) were rejected.
  `http://127.0.0.1/` and `http://localtest.me/` (resolves to 127.0.0.1)
  were blocked. This check found and fixed a defect: the fetcher used only
  the first resolved address, so IPv6-first hosts timed out on an
  IPv4-only network. It now tries validated addresses IPv4-first, with
  connect-failure fallback.

**Not verified**

- Behaviour against a real DNS-rebinding server was not exercised. The
  mitigation is covered by unit tests with a rebinding resolver stub and by
  the design (single resolution per hop, connect to the validated IP).
- DNS timeout behaviour is covered by unit tests with a stalled resolver
  stub, not against a real slow DNS server.

**Review follow-ups (2026-10-24, PR review)**

- Session lifecycle: `run_research` now takes a session factory and uses a
  read-only session, a fetch with no session open, and one narrow write
  transaction. It no longer commits a caller's session. Tests assert that no
  pooled connection is checked out during the fetch and that a caller's
  pending changes are never committed.
- DNS bound: resolution is limited to `min(connect timeout, remaining
  budget)` on a bounded worker pool, so the total fetch budget now covers
  name resolution. A stuck OS lookup still occupies its worker until it
  returns (see `docs/architecture.md`).

## P3 — Explorer: qualification and saved prospects

**Scope:** rule-based qualification scoring with rationale; saved prospect
model and API; search/filtering of saved prospects; deduplication rules.

**Implementation notes (2026-10-24)**

- Rule engine: `app/services/qualification_rules.py`, version
  `qualification-rules/v1`. It is pure Python (no LLM, database, network,
  or clock) and uses three fixed-weight factors: `industry_fit` 50,
  `location_fit` 30, `exclusion_check` 20. Absence of a term counts as
  evidence only when a complete research page was read. Statuses are
  `qualified`, `unqualified`, and `insufficient_evidence` (coverage below
  `min_evidence_coverage`). Full rules are in `docs/architecture.md`.
- Criteria: `QualificationCriteria`, a validated, canonicalized
  target-market profile (keywords, locations, TLDs, exclusions, threshold,
  minimum coverage). It is supplied per request, with documented defaults
  (`default-b2b-software`, threshold 60, coverage 0.5).
- Persistence (migration `0003_qualification_prospects`, additive):
  - `company_qualifications` is append-only and stores the rule version,
    criteria, score, status, threshold, coverage, factors with evidence,
    rationale, and timestamps.
  - `saved_prospects` has a unique `candidate_id` and an FK to the
    justifying qualification.
- API:
  - `POST /companies/{id}/qualify` (201).
  - `GET /companies/{id}/qualifications`.
  - `POST /companies/{id}/save`: 201 when created, 200 with the existing
    prospect unchanged.
  - `GET /prospects`: search, filters, deterministic sort, bounded
    pagination.
  - `GET /prospects/{id}`.
- Saving is enforced in the service layer. It requires a persisted
  `qualified` run whose score is at or above its recorded threshold.
  Duplicates are prevented by the unique constraint plus
  `ON CONFLICT DO NOTHING`.
- `CandidateId` moved to `app/routers/params.py` so all routers share one
  definition.

**Exit criteria**

- [x] Qualification thresholds and rationale are persisted and auditable:
      every run is a new row with rule version, canonical criteria, factor
      evidence, and rationale.
- [x] Saving a prospect requires qualification output. The backend refuses
      saves with no run (409 `qualification_required`) and with
      `unqualified` or `insufficient_evidence` runs (409
      `prospect_not_qualified`), and it re-checks score ≥ threshold.
- [x] Duplicate prospects are prevented deterministically: a unique
      constraint, `ON CONFLICT DO NOTHING`, and the existing prospect
      returned unchanged. The race path is covered by a test that simulates
      a concurrent insert.
- [x] End-to-end flow tested with fakes: discovery (`FakeSearchProvider`) →
      research (`FakeWebsiteFetcher`) → qualify → save → search → detail
      (`tests/integration/test_prospects_api.py`).
- [x] Migration `0003` upgrades a Phase 2 database with candidates and
      research without loss, and downgrades back (automated test on
      `explee_test`). `alembic check` reported "No new upgrade operations
      detected" on a fresh `explee_test` at head and on the configured
      `explee` database after `alembic upgrade head`
      (`0002 -> 0003`, 2026-10-24).
- [x] Full suite on 2026-10-24 (Python 3.14.8, PostgreSQL 17,
      `TEST_DATABASE_URL` → `explee_test`): 567 passed, with the existing
      Starlette/httpx deprecation warning. Without a test database: 442
      passed, 125 skipped.
- [x] Phase 0–2 regression tests pass. Fixture updates only: truncate and
      drop the new tables, and the head revision is now `0003`.
- [x] No Mistral, outreach, UI, authentication, Docker, or background
      workers added; no new dependencies.

**Not verified**

- The configured `explee` database contained no candidate or research
  rows when it was upgraded, so preservation of real data was verified only
  by the automated migration test on `explee_test`.
- True simultaneous saves from two processes were not exercised; the race
  is simulated in one process, and the database constraint is tested
  directly.

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
