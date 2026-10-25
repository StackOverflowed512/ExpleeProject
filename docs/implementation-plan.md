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
  `qualification-rules/v2`. It is pure Python (no LLM, database, network,
  or clock) and uses three fixed-weight factors: `industry_fit` 50
  (required), `location_fit` 30, `exclusion_check` 20. Evidence has a level
  (`website`, `domain`, or `discovery`), and terms are looked up on the
  website first. Absence counts only when a complete research page was
  read. Statuses are `qualified` (industry fit confirmed by the company's
  website), `preliminary` (score meets the threshold but industry fit
  rests only on search-result data; not saveable), `unqualified`, and
  `insufficient_evidence`. Full rules are in `docs/architecture.md`.
- Criteria: `QualificationCriteria`, a validated, canonicalized
  target-market profile (keywords, locations, TLDs, exclusions, threshold,
  minimum coverage). It is supplied per request, with documented defaults
  (`default-b2b-software`, threshold 60, coverage 0.5). Excluded keywords
  may not repeat target terms.
- Persistence (migrations `0003_qualification_prospects`, additive, and
  `0004_qualification_preliminary`, which only widens the status check
  constraint):
  - `company_qualifications` is append-only and stores the rule version,
    criteria, score, status, threshold, coverage, factors with evidence,
    rationale, and timestamps.
  - `saved_prospects` has a unique `candidate_id` and an FK to the
    justifying qualification, stated explicitly as `ON DELETE NO ACTION`.
- API:
  - `POST /companies/{id}/qualify` (201).
  - `GET /companies/{id}/qualifications`: `total`, `limit`, `latest`,
    and `history`.
  - `POST /companies/{id}/save`: 201 when created, 200 with the existing
    prospect unchanged.
  - `GET /prospects`: search, filters, deterministic sort, bounded
    pagination.
  - `GET /prospects/{id}`.
- Saving is enforced in the service layer. It requires a persisted v2
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
      `preliminary`, `unqualified`, `insufficient_evidence`, or v1 runs
      (409 `prospect_not_qualified`), and it re-checks score ≥ threshold.
- [x] Duplicate prospects are prevented deterministically: a unique
      constraint, `ON CONFLICT DO NOTHING`, and the existing prospect
      returned unchanged. The race path is covered by a test that simulates
      a concurrent insert.
- [x] End-to-end flow tested with fakes: discovery (`FakeSearchProvider`) →
      research (`FakeWebsiteFetcher`) → qualify → save → search → detail
      (`tests/integration/test_prospects_api.py`).
- [x] Migration `0003` upgrades a Phase 2 database with candidates and
      research without loss. Migration `0004` upgrades a `0003` database
      containing candidates, research, qualifications, and a prospect
      with all rows intact. A downgrade is refused while `preliminary`
      rows exist. All of this is automated on `explee_test`.
      `alembic upgrade head` (`0001 -> 0004`) followed by `alembic check`
      on a freshly reset `explee_test` reported "No new upgrade operations
      detected" (2026-10-24).
- [x] Full suite after the second review round (2026-10-24, Python
      3.14.8, PostgreSQL 17, `TEST_DATABASE_URL` → `explee_test`): 612
      passed, with the existing Starlette/httpx deprecation warning.
      Without a test database: 474 passed, 138 skipped.
- [x] Phase 0–2 regression tests pass. Fixture updates only: truncate and
      drop the new tables, and the head revision is now `0004`.
- [x] No Mistral, outreach, UI, authentication, Docker, or background
      workers added; no new dependencies.

**Not verified**

- The configured `explee` database contained no candidate or research
  rows when it was upgraded, so preservation of real data was verified only
  by the automated migration test on `explee_test`.
- True simultaneous saves from two processes were not exercised; the race
  is simulated in one process, and the database constraint is tested
  directly.
- Migrations were verified only on the disposable `explee_test`
  database. The configured `explee` development database reports
  `0004_qualification_preliminary` (it was upgraded outside these
  sessions) and holds no candidate or research rows, so real-data
  preservation rests on the automated migration tests.
- SonarCloud: the PR's quality gate failed on "Reliability Rating on New
  Code" for both pushed commits. The project is private and the findings
  could not be read without a SonarCloud sign-in. Exact float equality
  in tests (rule `python:S1244`, a reliability rule) was fixed with
  `pytest.approx`; whether that clears the gate is unverified until the
  branch is pushed and re-analysed.

**Review follow-ups (PR #8, 2026-10-24)**

- Rules v1 → v2: a keyword in a search-result title or snippet (for
  example a listicle such as "Best Software Companies in Berlin") could
  produce `qualified` without website research. v2 treats discovery
  matches as preliminary, discards them once a complete page refutes
  them, and requires website-confirmed industry fit for `qualified`.
  Only v2 runs can justify a save.
- History response now includes `limit`.
- `matched_signals` and `missing_signals` are de-duplicated, and a term
  matched by any factor is not reported as missing.
- `saved_prospects.qualification_id` states `ON DELETE NO ACTION`
  explicitly (equal to the existing database behaviour, so no FK
  migration; `alembic check` is clean). `NO ACTION` was chosen over
  `RESTRICT` so candidate deletion can still cascade.
- Stored criteria are returned through a lenient `StoredCriteria`
  schema, so tightened input validation (such as the new overlap rule)
  can never make older runs unreadable.
- Second round: the history contract (`candidate_id`, `total`, `limit`,
  `latest`, `history`) is pinned by API, validation, and OpenAPI tests
  and documented with an example. Signal lists are de-duplicated inside
  each factor (one evidence item and one list per term, even for
  non-canonical criteria) and across factors. The new response-level
  `unconfirmed_signals` aggregate reports each term once, in the
  strongest applicable list. Rule semantics are unchanged for valid
  criteria, so the version stays `qualification-rules/v2`.

## P4 — Outreach: campaigns and drafts

**Scope:** campaign model built from saved prospects; Mistral adapter behind
the `DraftGenerator` protocol (the earlier `AIProvider` name was not used);
draft generation with versioned prompts; draft states.

**Implementation notes (2026-10-24)**

- Campaigns: `campaigns` and `campaign_prospects` (composite PK prevents
  duplicate links, `position` keeps selection order). Only existing saved
  prospects can be added (422 `invalid_prospect_ids`). Campaign text that
  feeds the prompt may not contain email addresses or placeholders.
- Generator: `DraftGenerator` protocol in
  `app/services/providers/drafts.py`. The Mistral adapter
  (`mistral.py`, HTTPX, no SDK, JSON mode, bounded timeouts and retries,
  safe error mapping) and a deterministic fake are selected by
  `DRAFT_GENERATOR`. `MISTRAL_API_KEY` is optional at startup; a missing
  key yields `provider_not_configured` (503).
- Prompt: `outreach-draft/v1` in `app/services/outreach_prompt.py`. It is a
  pure, deterministic function with a SHA-256 audit hash. Prospect data is
  sanitized, length-capped, JSON-escaped, and fenced as untrusted, and the
  model is told not to invent facts. Output is validated independently
  (subject and body required, length bounds, no email addresses, no
  placeholders).
- Drafts: `outreach_drafts`. `state` is constrained to `pending`. Rows
  record the prompt version and hash, provider, model, safe response
  metadata (response ID, finish reason, token usage; not the raw body),
  provenance (candidate, qualification, research, evidence sources), and
  a `revision` per campaign prospect.
- Generation: the read session closes before provider calls, and each
  validated draft is committed in its own transaction. At most 10 drafts
  are generated per request (`remaining` reports the rest). Without
  `regenerate`, prospects that already have a draft are skipped
  (idempotent retries). With `regenerate`, a new revision is added and
  never overwrites. A unique constraint plus `ON CONFLICT DO NOTHING`
  handles races.
- API: `POST/GET /campaigns`, `GET /campaigns/{id}`,
  `POST/GET /campaigns/{id}/drafts`, `GET /drafts/{id}`. There is no
  send, approve, or email code; a test asserts that no module imports an
  email library and that no route can send or approve.
- Migration `0005_campaigns_drafts` (additive). Its CHECK constraints are
  named with `op.f()` so they match the models.

**Exit criteria**

- [x] Mistral is reachable only through the adapter, and tests use a fake.
      Adapter behaviour is unit-tested with HTTPX mock transports.
- [x] Drafts persist the prompt version, model, and response metadata.
      Only safe fields are stored; the raw provider body is deliberately
      not persisted.
- [x] Generation failures never produce a partially saved draft. This is
      tested for provider errors, missing key, malformed or empty output,
      mid-batch failure, and database rollback.
- [x] No credentials are required to run the suite.
- [x] Migration `0005` upgrades a `0004` database containing candidates,
      research, qualifications, and a saved prospect without loss, and
      downgrades back (automated on `explee_test`). `alembic upgrade head`
      (`0001 -> 0005`) followed by `alembic check` on a freshly reset
      `explee_test` reported "No new upgrade operations detected"
      (2026-10-24).
- [x] Full suite (2026-10-24, Python 3.14.8, PostgreSQL 17,
      `TEST_DATABASE_URL` → `explee_test`): 777 passed, with the existing
      Starlette/httpx deprecation warning. Without a test database:
      593 passed, 184 skipped. Phase 0–3 regression tests pass;
      fixtures were only extended for the new tables and settings.
- [x] No email sending, approval endpoints, UI, authentication, Docker, or
      background workers; no new dependencies.

**Not verified**

- No live Mistral request was made: no `MISTRAL_API_KEY` was configured in
  this environment. The request and response contract follows the official
  API reference and is covered by mocked tests; a live check remains
  manual.
- Migrations were not applied to the configured `explee` development
  database. Run `alembic upgrade head` there before using the new
  endpoints.
- Concurrent generation is tested with two threads, each with its own
  database sessions, racing against PostgreSQL
  (`tests/integration/test_outreach_concurrency.py`). Separate OS
  processes were not used; the threads exercise the same database-level
  conflict path.

**Follow-up: real concurrency tests (2026-10-24, PR #9 review)**

- A barrier in the fake generator makes two `generate_drafts` calls both
  read the database and call the provider before either persists.
- **Default mode** (already correct): exactly one draft is stored; one
  call reports `generated` and the other `existing`, and both return the
  same persisted draft.
- **Bug found and fixed for `regenerate`:** concurrent requests computed
  the same next revision, and the loser's freshly generated draft was
  discarded and reported as `existing`. Now a regenerate request that
  loses the race moves on to the next revision, so each request gets its
  own revision, numbered 1..n with no gaps or duplicates (verified over
  repeated races). It gives up safely with `draft_persistence_failed`
  after 5 collisions, and that path is tested too.
- The concurrency tests passed in five consecutive runs.

**Follow-up: CHECK constraint names (migration `0006`, 2026-10-24)**

- Migrations `0002`–`0004` created their six CHECK constraints with
  doubled names, because Alembic applied the naming convention to names
  that were already final. For example:
  `ck_company_qualifications_ck_company_qualifications_score_range`, and
  `..._sta_abd7` where the 63-character limit forced a truncated MD5
  suffix. The constraints were always enforced, but their names differed
  from the models and from `create_all` databases. `alembic check` does
  not compare CHECK constraints, so it never reported this.
- `0006_check_constraint_names` renames them to the model names with
  `ALTER TABLE ... RENAME CONSTRAINT`. Definitions and data are
  unchanged. Each rename happens only when the doubled name exists, so
  the migration is a no-op on databases whose names are already
  correct.
- The downgrade restores the exact doubled names, computed with
  SQLAlchemy's truncation rule. Migration `0004`'s downgrade depends on
  those names.
- Migrations `0001`–`0005` were not edited.
- Verified on `explee_test`:
  - after upgrading, every table's CHECK names equal the model names;
  - downgrading to `0005` restores the original names, with rows intact
    and constraints still enforced;
  - a full downgrade to `0002` still works;
  - a `create_all` database stamped at `0005` upgrades unchanged.

## P5 — Outreach: approval and delivery

**Scope:** explicit approval transition enforced by the backend; email
provider protocol and one real adapter; send audit trail; dry-run path.

**Implementation notes**

- **No authentication.** Approval is an explicit, recorded action in a
  local single-user workflow. `approved_by` is the configured
  `APPROVAL_ACTOR` label, not a verified identity.
- **Approval** (`app/services/approvals.py`, `draft_approvals`):
  - `POST /drafts/{id}/approve` takes an explicit recipient and the
    draft revision.
  - It records the revision, the normalized recipient, a SHA-256 hash of
    draft id, revision, recipient, subject and body, the actor, and the
    time.
  - The draft row is locked during approval, and a draft has at most one
    approval (unique `draft_id`).
  - An identical repeat returns the stored approval (200). A different
    recipient (`draft_already_approved`) or a stale revision
    (`draft_revision_mismatch`) is refused, never silently changed.
  - Recipients are never guessed or discovered.
- **Delivery** (`app/services/delivery.py`, `email_send_attempts`):
  - `POST /drafts/{id}/send` accepts only `mode` (`dry_run` or `deliver`).
  - The service locks the draft, checks the state, re-verifies the
    approval and recomputes the content hash. It checks the config
    (`EMAIL_DRY_RUN`, sender), checks that no other revision for the same
    campaign prospect was delivered, then claims the send: an
    `in_progress` attempt with a stable Message-ID, and the draft moves to
    `sending`. This all happens in one transaction.
  - The provider is then called with no transaction or row lock held
    (only the sender's advisory-lock connection, see the second review
    follow-up), and the result is finalized with conditional updates.
  - A dry run records an attempt and never calls a provider.
  - A definite failure records `failed` with a category, and the draft
    returns to `approved` (`retry_safe: true`).
  - A transport error after the message content was sent, or an
    unexpected error, records `uncertain`, and the draft becomes
    `delivery_uncertain`. It is never retried automatically.
  - `POST /drafts/{id}/resolve-delivery` records a human-verified outcome
    for an uncertain send, or for a `sending` claim older than 15 minutes
    (the process died mid-send). It is refused while a sender still holds
    the draft's PostgreSQL advisory lock, however old the claim.
  - Repeating a send after success returns `already_sent` without calling
    the provider.
- **Database guards** (migration `0007_approval_delivery`):
  - the draft state check is widened to `pending`, `approved`, `sending`,
    `sent`, `delivery_uncertain`;
  - a partial unique index allows only one `in_progress` attempt per
    draft;
  - a partial unique index allows only one delivering or delivered draft
    per campaign prospect;
  - CHECKs tie attempt outcomes to the mode, the finish time and the
    failure category.
  - Existing Phase 4 drafts stay `pending`. The downgrade refuses (and
    rolls back) if any draft has left `pending`. Migrations `0001`–`0006`
    were not edited.
- **Providers:**
  - `EmailProvider` protocol, with `FakeEmailProvider` and
    `SmtpEmailProvider`, the latter built on stdlib `smtplib`/`ssl`/`email`.
  - SMTP uses STARTTLS (required when selected) or implicit TLS, with
    `ssl.create_default_context()` (certificate and hostname
    verification), and a bounded timeout.
  - Failures map to `email_connection_failed`, `email_tls_failed`,
    `email_authentication_failed`, `email_timeout` and `email_rejected`.
    Ambiguous post-DATA failures map to `email_delivery_uncertain`.
  - Credentials, bodies and recipients are never logged; `__repr__` hides
    the password.
- **Safe defaults:** `EMAIL_PROVIDER=fake` and `EMAIL_DRY_RUN=true`.
  Real delivery requires `EMAIL_PROVIDER=smtp`, `EMAIL_DRY_RUN=false`,
  `EMAIL_SENDER` and `SMTP_HOST`. The settings are validated at startup:
  the sender address, the host, a port of 1–65535, a timeout of
  0–120 seconds, and the username and password set together.
- `GET /drafts/{id}` now includes `approval` and `send_attempts`.

**Exit criteria**

- [x] Sending requires a recorded approval that matches the exact draft
      revision, recipient and content. The service enforces this for every
      caller, and tampering with stored content or the approval is
      refused (`approval_content_mismatch`).
- [x] Sends are idempotent per draft and fully audited: an append-only
      attempt row with mode, provider, outcome, failure category,
      Message-ID, start and finish times, and duration. Two concurrent
      sends of one draft deliver exactly once (PostgreSQL, two threads).
- [x] Failure handling and the retry policy are documented
      (`docs/architecture.md`, README 5f) and tested: definite failures
      are retryable, uncertain outcomes are blocked until manually
      resolved, and an interrupted `sending` claim is recoverable only
      after it goes stale *and* no live sender holds the draft's lock.
- [x] No email is sent in tests. Tests use the fake provider or a mocked
      `smtplib` client, and a dry-run test blocks sockets to prove no
      connection is attempted.
- [x] Migration `0007` upgrades a `0006` database containing Phase 4
      drafts without loss, and downgrades (automated on `explee_test`).
      `alembic upgrade head` (`0001 -> 0007`) followed by `alembic check`
      on a freshly reset `explee_test` reported "No new upgrade operations
      detected" (2026-10-24).
- [x] Full suite (2026-10-24, Python 3.14.8, PostgreSQL 17,
      `TEST_DATABASE_URL` → `explee_test`): 922 passed, with the existing
      Starlette/httpx deprecation warning. Without a test database:
      688 passed, 234 skipped. Phase 4 tests that asserted "no sending
      exists" were replaced by stricter Phase 5 equivalents (sending only
      at `/drafts/{id}/send`; email libraries only in the SMTP adapter).
- [x] No UI, authentication, bulk or scheduled sending, background
      workers, contact discovery, Mistral changes, Docker, or new
      dependencies.

**Not verified**

- No live SMTP delivery was performed; no SMTP server or credentials were
  configured. The adapter is covered by mocked `smtplib` tests. The manual
  procedure is in README 5f.
- The configured `explee` development database was not migrated to
  `0007`. Run `alembic upgrade head` there before using the new endpoints.
- Concurrent sends were tested with threads, each with its own database
  sessions, not with separate OS processes. Both go through the same
  database row lock and unique-index conflict path.

**Follow-up: PR #10 review (2026-10-25)**

- **P1 fixed: the fake provider could mark a draft `sent`.** With
  `EMAIL_PROVIDER=fake` and `EMAIL_DRY_RUN=false`, a `deliver` request
  ran through the fake provider and recorded a real send. Providers now
  declare `delivers_email`: `True` only for the SMTP adapter, and the
  configured fake is always `False`. The service refuses `deliver` with
  409 `delivery_disabled` before claiming anything, so no attempt is
  recorded and the draft stays `approved`. Regression tests cover this at
  the service and API level, using the provider exactly as
  `build_email_provider` configures it.
- **P2 fixed: audit history could be deleted by cascade.** The foreign keys
  from `draft_approvals` and `email_send_attempts` are now
  `ON DELETE RESTRICT`. Deleting a draft, campaign, or approval that has
  history is refused, and a database test proves the history survives.
  Migration `0007` was edited in place: it was unmerged, and no persistent
  database had applied it (the dev database is at `0006`; `explee_test`
  is rebuilt by the tests).
- Full suite (`TEST_DATABASE_URL` → `explee_test`): 924 passed. Without a
  test database: 688 passed, 236 skipped. `alembic upgrade head` and
  `alembic check` on a freshly reset `explee_test`: no pending operations.

**Follow-up: PR #10 second review (2026-10-25)**

- **P2 fixed: a slow but live send could be resolved and sent twice.** A
  `sending` claim older than 15 minutes was treated as interrupted, but
  with up to 120 s per SMTP step a healthy send can run longer. Now a real
  send holds a PostgreSQL advisory lock on the draft id for the whole
  claim → deliver → finalize sequence. The lock is on its own autocommit
  connection, so no transaction stays open during the network call.
  `resolve-delivery` refuses while the lock is held. A crashed process
  releases it automatically.
- New tests (real PostgreSQL):
  - a claim far past the threshold with a live, blocked sender cannot be
    resolved, and the send then completes once;
  - a lock held by another connection (standing in for another process)
    blocks both send and resolve, and recovery works once it is released;
  - the lock is released after sent, failed, and uncertain outcomes, and
    when the claim is refused;
  - dry runs take no lock.
- Full suite (`TEST_DATABASE_URL` → `explee_test`): 931 passed. Without a
  test database: 688 passed, 243 skipped. `alembic upgrade head` and
  `alembic check` on a freshly reset `explee_test`: no pending operations.

## P6 — Web interface

**Scope:** Jinja2 templates and HTMX interactions for Explorer and Outreach;
server-rendered pages calling existing services.

**Implementation notes**

- **Where it lives.** The interface is served by the same app under `/ui`.
  Routes are in `app/routers/ui/`, infrastructure in `app/web/`, templates
  in `app/templates/` and assets in `app/static/`. The JSON API contracts
  are unchanged. See *Web interface (P6)* in `docs/architecture.md`.
- **Pages:**
  - Explorer: discovery search, recent candidates, and a company page with
    provenance, research and refresh, qualification with criteria and
    evidence, and save.
  - Saved prospects: filters and detail.
  - Campaigns: list, create and detail, with explicit draft generation.
  - Draft review: a queue by state, and a review page with the message,
    provenance, approval, delivery, recovery and attempt history.
  - Delivery history, with filters.
  - System status: configuration and migration state, with no secrets.
- **Same backend workflows.** Routes call the existing services; templates
  hold no business rules. Display hints in `app/web/presentation.py` reuse
  `approvals.approval_matches`, and every action is re-checked by the
  services.
- **Approval and sending stay separate.**
  - Approve, send and resolve are separate `POST` requests, each with an
    explicit confirmation.
  - Field allow-lists refuse any posted recipient, subject or body on send,
    and any subject or body on approve.
  - Nothing happens on a `GET` request. Generation, research and
    qualification also run only on an explicit `POST`.
- **Security.**
  - Signed double-submit CSRF tokens on every UI `POST` (form field or HTMX
    header), plus Origin and `Sec-Fetch-Site` checks.
  - Bounded, standard-library form parsing.
  - Redirects only to fixed internal paths, and whitelisted notice codes.
  - A strict CSP and safe headers on `/ui`.
  - Autoescaped templates, and HTML error pages that never show exception
    details.
  - Content-hashed asset URLs.
- **Backend changes (read-only, no rule changes):**
  - `discovery.list_candidates` and `to_candidate_summary`;
  - `prospects.find_saved_prospect`;
  - `drafts.list_drafts_overview` and `drafts.count_drafts_by_state`;
  - `delivery.list_attempts_overview`;
  - `DRAFT_ERRORS` made public in `app/routers/drafts.py`;
  - `/ui` error delegation in `app/errors.py`;
  - `GET /` lists `ui`.
- **Assets and dependencies.**
  - Jinja2 3.1.6 is now a declared dependency.
  - HTMX 2.0.10 is vendored in `app/static/vendor/` (0BSD; the same SHA-256
    from two CDNs).
  - Tailwind CSS v4.3.3 is compiled with the standalone CLI (no Node.js;
    the binary was verified against the release's `sha256sums.txt` and is
    kept in the git-ignored `tools/`), via `python -m scripts.build_css`.
  - The compiled `app/static/css/app.css` is committed.
- **Docs corrected:** `docs/architecture.md` still said that
  `EMAIL_PROVIDER=fake` with dry run off records a `sent` attempt; since
  the PR #10 fix it is refused.

**Exit criteria**

- [x] Routes remain thin; templates contain no business rules. UI routes
      parse, validate with the existing schemas, call services, and
      render.
- [x] The approval UI calls the backend approval service and cannot bypass
      it. Tests cover:
      - smuggled fields refused, with the sent message identical to the
        draft;
      - an unapproved send refused;
      - a fake provider never really delivers;
      - an uncertain delivery blocks resending;
      - page loads never approve or send.
- [x] Accessibility basics and error rendering are covered:
      - labels on every input, with `aria-invalid` and `aria-describedby`
        for errors;
      - a skip link, `aria-current` navigation, visible focus rings, and
        text labels on every status;
      - HTML error pages for 403, 404, 409, 413, 415, 422, 500 and 503.
- [x] CSRF and safe rendering are tested: every UI `POST` route needs a
      token; wrong, foreign and cross-origin requests are refused; the
      cookie attributes, headers and CSP are checked; there are no inline
      scripts; secrets are never rendered; output is escaped.
- [x] Phase 0–5 regression tests pass. Full suite (2026-10-25, Python
      3.14.8, PostgreSQL 17, `TEST_DATABASE_URL` → `explee_test`): 1030
      passed (99 new), with the existing Starlette/httpx deprecation
      warning. Without a test database: 734 passed, 296 skipped.
      `alembic upgrade head` and `alembic check` on a freshly reset
      `explee_test`: no pending operations. No migration was added.
- [x] No authentication, bulk or scheduled sending, workers, new frontend
      framework, Docker, or new discovery, research or qualification rules.

**Not verified**

- The interface was checked manually in a browser (desktop and a 375 px
  mobile viewport) against the disposable `explee_test` database with fake
  providers: an approval was submitted through HTMX, the console showed no
  errors, and no page scrolled horizontally. No automated browser or
  screen-reader tests were run.
- No live Tavily, Mistral, or SMTP call was made from the UI. The UI uses
  the same services that the earlier phases tested with mocks.
- The UI was not run against the development database `explee`. It is at
  `0007` with no rows (read-only check), so it is ready for the UI.

**Follow-up: PR #11 code review (2026-10-25)**

- Campaign page status was capped at the first 100 drafts. It now uses
  `drafts.campaign_draft_summary`: the latest revision plus a count per
  prospect, with no row cap. Tested with 35 prospects and 105 drafts.
- The CSRF cookie is persistent (180 days), so tabs restored together share
  one nonce. Two pages loading at the same moment on a browser's very first
  visit can still race; the losing tab shows "reload the page", and nothing
  changes.
- HTMX fragment requests that fail get an error the size of their target
  (`_error_fragment.html`), never a whole page nested inside the page.
- Research notices fall back to a generic notice for unknown outcomes.
- Draft errors are mapped through the class hierarchy (`draft_error`), so
  subclasses keep their 409 or 5xx code in both the API and the UI.
- Fragments skip the attention-count query and the delivery-mode banner,
  the delivery poll loads only the delivery context, and a database outage
  no longer triggers a second connection attempt for the error page.
- Cleanup: `run_discovery` reuses `to_candidate_summary`; the unused
  `optional_text` is removed; `scripts/build_css.py` takes `--tailwind
  PATH` instead of reading an environment variable.
- Full suite (`TEST_DATABASE_URL` → `explee_test`): 1037 passed. Without a
  test database: 738 passed, 299 skipped. `alembic check` is clean.

**Follow-up: UI/UX overhaul (2026-10-25)**

A visual redesign of every page on top of the Phase 6 architecture. This is
not plan phase P7 (*Reliability and operations*, below), which is unchanged.

- **Design system** in `app/static/src/app.css`:
  - tokens for a warm neutral palette and an indigo-violet accent, radii,
    shadows and focus rings;
  - components for the shell, page headers and breadcrumbs, cards, buttons
    (`btn-danger` only for real email), forms, filter toolbars, badges,
    alerts (`alert-critical` for uncertain delivery), the email message
    sheet, tables, tabs, empty states and stat cards;
  - `prefers-reduced-motion` is honoured.
- **Templates:** an `icon()` macro (inline SVG, `aria-hidden`); new
  `breadcrumb`, `stat` and `score` macros; icons in alerts and empty states;
  spinners on submit buttons; and a CSS-only page progress bar. Every
  existing page is redesigned with its routes, form fields, HTMX targets,
  status codes and safety wording unchanged.
- **Overview page** (`/ui/overview`; `/ui` now redirects there): read-only
  counts, the review queue, recent attempts and `presentation.next_steps`.
  No backend or schema changes.
- **Fix:** `hx-disabled-elt` moved from `<body>` to each form. Inherited by
  links, it made HTMX log errors on every navigation.
- **Tests:**
  - the root redirect assertion now expects `/ui/overview`, and the page
    sweep includes the Overview;
  - added tests for the Overview, `next_steps`, the icon macro, no `| safe`
    in templates, `hx-disabled-elt` on forms only, and the compiled
    design-system classes.
- **Results:** full suite (`TEST_DATABASE_URL` → `explee_test`) 1049 passed;
  without a test database 748 passed, 301 skipped; `alembic check` clean.
- **Manual check:** pages were inspected in a browser at 1100 px and 375 px
  against `explee_test` with fake providers: no horizontal scroll, and the
  console is clean after the `hx-disabled-elt` fix. No automated visual
  regression or screen-reader tests exist.

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
