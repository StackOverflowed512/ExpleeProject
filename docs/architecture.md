# Architecture

Phase 0 establishes the skeleton. The target is a **modular monolith**: one
FastAPI process, one PostgreSQL database, and internal module boundaries that
can later be extracted or replaced without touching business logic.

## Layering

```
HTTP (app/routers)          thin: parse, validate, delegate, serialize
  └─ Services (app/services) business rules; framework-agnostic where practical
       └─ Providers          replaceable adapters (web search, enrichment, Mistral, email)
            └─ Models (app/models) SQLAlchemy 2.x ORM against PostgreSQL
Infrastructure              app/config.py, app/database.py, app/errors.py, app/logging_config.py
```

Rules:

- **Routers stay thin.** A route validates its inputs, calls a service, and
  returns a Pydantic schema. It does not contain business rules.
- **Services own decisions.** Qualification thresholds, deduplication,
  approval rules, and prompt construction live in services and are unit
  testable without HTTP or a live database.
- **Providers are interfaces.** Every external capability (search API,
  enrichment source, Mistral, email transport) is defined by a small
  protocol and backed by a concrete adapter selected through configuration.
  Services depend on the protocol, so tests inject fakes and swapping
  vendors never rewrites business rules.
- **Errors are uniform.** All failures surface as
  `{"error": {"code", "message", "details"}}` via handlers in
  `app/errors.py`. Domain code raises `AppError` with a stable code.
- **Configuration is explicit.** `app/config.py` is the only place that
  reads the environment. Nothing in the codebase hardcodes secrets.

## Runtime shape

`create_app(settings)` builds a FastAPI instance, configures logging,
creates one SQLAlchemy engine and session factory on `app.state`, installs
exception handlers, and mounts routers. Sessions are created per request
by the `get_session` dependency and closed when the request ends.
Engine creation is lazy about connections: importing the app or calling
`GET /health` never opens a database connection.

The process-wide `app = create_app()` exists for `uvicorn app.main:app`.
Tests call `create_app(Settings(_env_file=None, ...))` instead, so they
never read the developer's `.env`.

## Planned modules and boundaries

### Explorer

1. **Discovery** — implemented in P1; the real adapter moved from Brave to
   Tavily in P1.1. A `SearchProvider` protocol
   (`app/services/providers/search.py`) accepts a natural-language query and
   a bounded limit and returns normalized `SearchResultItem` records. Two
   adapters exist: `tavily` (default real API) and `fake` (deterministic,
   used by tests and credential-free runs). Selection happens through
   `SEARCH_PROVIDER`; discovery code never imports a vendor SDK or reads a
   vendor payload. `TAVILY_API_KEY` is optional at startup and validated by
   the adapter when a search is requested, so health checks and unrelated
   tests run without credentials. A missing key raises
   `provider_not_configured`; selection never falls back silently.
2. **Research** — implemented in P2. A `WebsiteFetcher` protocol
   (`app/services/providers/website.py`) fetches the website URL stored on
   a persisted candidate and returns a normalized `FetchResult`. Adapters:
   `httpx` (real HTTP with SSRF protections) and `fake` (deterministic, no
   network), selected by `WEBSITE_FETCHER`. Research does not use Tavily
   or any search API. Every attempt is stored in `company_research` with
   its provenance.
3. **Enrichment** — implemented in P2 as a read-only projection that
   combines a candidate's discovery fields with its latest usable research.
   No facts are inferred from page text.
4. **Qualification** — implemented in P3 as deterministic, versioned
   Python rules evaluated against a caller-supplied target-market profile.
   Every run is persisted with its criteria, factors, evidence, score, and
   rationale so a decision can be audited. No LLM is involved.
5. **Saved prospects** — implemented in P3. A candidate can be saved only
   with a persisted `qualified` result, at most once.
6. **Persistence** — `company_candidates` stores discovered companies
   through the discovery service, which enforces deduplication and source
   provenance. `company_research` stores research attempts per candidate,
   `company_qualifications` stores every qualification run, and
   `saved_prospects` stores one row per saved candidate.

Providers must never invent data: a field is either sourced (with a URL or
provider name recorded) or absent.

### Discovery details (P1)

- **Normalization.** `app/services/discovery_urls.py` accepts only absolute
  http/https URLs without userinfo. Domains are lowercased, stripped of a
  trailing dot and a leading `www.`, converted to IDNA ASCII, and used as
  the deduplication key.
- **Deduplication.** Inside a batch: normalized domain, or
  `(provider, provider_result_id)` when a consumer of the provider supplies
  one. In the database: partial unique indexes on `domain` and on
  `(provider, provider_result_id)`, both `WHERE ... IS NOT NULL`, so rows
  without either never conflict. Names are never part of identity.
- **Concurrency.** Inserts use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING
  RETURNING id` inside a savepoint per candidate; the loser of a race reads
  the winner's row. Any other database failure rolls back the whole search.
- **Update policy.** A later search fills only fields that are currently
  `NULL` (website URL, description). First-seen name, source URL, and known
  values are never overwritten, preserving provenance.
- **Error mapping.** Provider failures use the shared envelope:
  `provider_not_configured` (503), `provider_authentication_failed` (502),
  `provider_unavailable` (502), `provider_rate_limited` (503),
  `provider_timeout` (504), and `discovery_persistence_failed` (500).
  Messages never contain keys or provider payloads.
- **Retry policy.** The Tavily adapter retries only transient failures
  (429, 5xx, timeouts, connection errors) with bounded exponential backoff
  and a bounded `Retry-After`. Authentication failures, other 4xx responses,
  and malformed payloads are not retried. The adapter requests
  `search_depth=basic` to keep cost and latency predictable.

### Research and enrichment details (P2)

**Workflow.** `POST /companies/{id}/research` → load the candidate → read
its stored `website_url` → syntax-validate it (no network) → if fresh usable
research exists and `refresh` is false, return it → otherwise call the
injected `WebsiteFetcher` → persist the attempt → prune history → return the
attempt and the latest usable research. No endpoint accepts a URL; the
request body is `{"refresh": bool}` with unknown fields forbidden.

**Modules.**

| Module | Responsibility |
| --- | --- |
| `app/services/providers/website.py` | `WebsiteFetcher` protocol, `FetchResult`, `FetchOutcome`, `FetchFailure`. |
| `app/services/providers/url_safety.py` | URL syntax checks, non-public address detection, resolution checks. |
| `app/services/providers/website_httpx.py` | Real fetcher: pinning, redirects, limits, failure mapping. |
| `app/services/providers/html_text.py` | BeautifulSoup (`html.parser`) title and visible-text extraction. |
| `app/services/providers/website_fake.py` | Deterministic fake keyed by URL; records calls. |
| `app/services/research.py` | Research service: freshness, persistence, history, rollback. |
| `app/services/enrichment.py` | Pure `build_enrichment` plus read-only `get_enrichment`. |
| `app/routers/research.py` | Thin routes for research and enrichment. |

**Outcomes.** `success` (complete HTML with readable text), `partial` (text
extracted but the body hit `RESEARCH_MAX_RESPONSE_BYTES` or the text hit
`RESEARCH_MAX_TEXT_CHARS`), `empty` (HTML fetched, no readable text), and
`failed` with one safe `failure_category`: `invalid_url`,
`disallowed_scheme`, `credentials_in_url`, `disallowed_port`,
`blocked_destination`, `dns_resolution_failed`, `timeout`,
`connection_failed`, `too_many_redirects`, `invalid_redirect`,
`http_error`, `unsupported_content_type`, `malformed_response`. Only
`success` and `partial` are *usable*. A fetch failure is a recorded outcome
(HTTP 200), not an API error; raw exceptions and response bodies never leave
the adapter.

**Research model (`company_research`).** One row per attempt: `id`,
`candidate_id` (FK, `ON DELETE CASCADE`), `requested_url` (the candidate's
stored URL), `final_url` (last URL requested after permitted redirects),
`fetched_at`, `fetcher`, `outcome`, `failure_category`, `http_status`,
`content_type`, `page_title` (≤500 chars), `extracted_text` (≤
`RESEARCH_MAX_TEXT_CHARS`, stored only for usable outcomes),
`content_truncated`, `created_at`, `updated_at`. Check constraints enforce
the outcome set and that `failure_category` is present exactly when
`outcome = 'failed'`. Index: `(candidate_id, fetched_at)`.

**History policy.** Append-only per attempt, so a later failure never
overwrites earlier research. After each insert, in the same transaction, the
service keeps the five most recent attempts (`HISTORY_LIMIT`) plus the
latest usable record and deletes the rest for that candidate. The latest
success is therefore always retained, and growth is bounded at six rows per
candidate. Concurrent requests for one candidate may both fetch and both
insert; pruning still bounds the result.

**Freshness.** Usable research fetched within `RESEARCH_FRESHNESS_HOURS`
(default 24) is returned with `reused: true` and no network call. Failed or
empty attempts never count as fresh. `{"refresh": true}` forces a fetch;
`RESEARCH_FRESHNESS_HOURS=0` disables reuse. There is no cache layer or
background queue.

**Provenance.** Research never modifies the candidate: `source_url`,
`provider`, `provider_result_id`, `discovered_at`, and `description` keep
their discovery values. Research provenance is `requested_url`,
`final_url`, `fetched_at`, and `fetcher` on each row.

**Session lifecycle and transactions.** `run_research` receives the session
factory (via the `get_session_factory` dependency), not a request-scoped
session, and runs three isolated steps:

1. A short read-only session loads the candidate and checks freshness, then
   closes.
2. The network fetch runs with **no** session, transaction, or pooled
   connection held.
3. A new session opens one narrow `session.begin()` transaction for the
   insert and pruning.

The service therefore never commits or rolls back a caller's work. Any
`SQLAlchemyError` in step 3 rolls back that transaction only and surfaces as
`research_persistence_failed` (500), leaving earlier rows intact. The read
endpoints (`GET .../research`, `GET .../enrichment`) use the usual
request-scoped session because they never perform network I/O.

**Enrichment.** `GET /companies/{id}/enrichment` returns the candidate's
discovery fields, a `discovery` provenance block, `research_status`,
`website` (latest usable research with text and provenance, or `null`), and
`last_attempt` (most recent attempt of any outcome). It is a pure function
of stored rows: no fetches, no writes, no timestamps generated, so repeated
calls return identical output. Page text is third-party content, not
verified facts; qualification and saved prospects are untouched (P3).

**Status values** (research and enrichment responses): `available`,
`partial`, `unavailable` (attempted, nothing usable), `not_researched`.

**Error mapping.** `candidate_not_found` (404), `candidate_website_missing`
(409, no network request), `validation_error` (422: bad path ID, unknown
body field, non-boolean `refresh`), `research_persistence_failed` (500).

#### SSRF protections

Stored website URLs originate from third-party search results and are
treated as untrusted. For the initial URL and every redirect target:

1. **Syntax** (`check_url`, no network): absolute `http`/`https` only; no
   userinfo (including an empty `@`); ports 80 and 443 only; no whitespace,
   control characters, or backslashes; at most 2048 characters. A host is
   either a canonical IP literal (IPv6 must be bracketed) or a DNS name with
   valid LDH labels, at least two labels, and a top-level label that starts
   with a letter. That rule rejects alternate numeric forms such as
   `2130706433`, `0x7f.1`, `0177.0.0.1`, and `127.1` before any resolver
   can interpret them. `localhost`, `*.localhost`, `*.local`, `*.internal`,
   and `*.home.arpa` are blocked by name. IDN hosts are converted to
   punycode.
2. **Address policy** (`is_public_address`): an address must be
   `is_global` and not private, loopback, link-local, multicast, reserved,
   or unspecified. IPv4 addresses embedded in IPv6 (IPv4-mapped, 6to4,
   Teredo, NAT64 `64:ff9b::/96`) are checked too. This blocks cloud
   metadata endpoints (`169.254.169.254`, `fd00:ec2::254`,
   `100.100.100.200`) and CGNAT space.
3. **Resolution** (`resolve_public_addresses`): the host is resolved once
   per hop and *every* returned address must pass the policy. One private
   record rejects the host.
4. **DNS pinning**: the request is sent to a validated IP address
   (`https://93.184.216.34/path`). The original hostname is sent in the
   `Host` header and as the TLS server name (HTTPX/httpcore `sni_hostname`
   extension), so certificates are still verified against the hostname.
   HTTPX never resolves the name itself, which closes the
   validation-to-connection gap that DNS rebinding exploits. IPv4
   addresses are tried first, and only connect errors or connect timeouts
   move on to the next validated address (at most four).
5. **Redirects** are never followed by HTTPX (`follow_redirects=False`).
   The fetcher follows at most `RESEARCH_MAX_REDIRECTS` and re-runs steps
   1–4 on each target, so a redirect to `127.0.0.1`, a metadata IP, a
   private-resolving name, or a disallowed scheme or port is refused. A
   same-host redirect is resolved again, so a rebind between hops is
   caught.
6. **Client hardening**: `trust_env=False`, so proxy variables are
   ignored (a proxy would resolve names itself). Keep-alive is off, so a
   TLS connection is never reused for another hostname that shares an IP.
   The request is GET-only with `Accept-Encoding: identity`.
7. **Bounds**: connect, read, write, and pool timeouts;
   DNS resolution runs on a small worker pool (8 per fetcher) and is
   abandoned after `min(RESEARCH_CONNECT_TIMEOUT_SECONDS, remaining
   budget)`, failing as `timeout`; the `RESEARCH_MAX_FETCH_SECONDS` total
   deadline is checked before each hop, each address, and each body chunk; the streamed body stops at
   `RESEARCH_MAX_RESPONSE_BYTES`; only `text/html` and
   `application/xhtml+xml` are parsed; text is capped at
   `RESEARCH_MAX_TEXT_CHARS`.
8. **Logging**: the service logs candidate ID, hostname, and outcome or
   failure category only. HTTPX's own request logging (which includes full
   URLs) is raised to WARNING in `app/logging_config.py`. Bodies and page
   text are never logged.

The research service applies step 1 before calling the fetcher, so an
invalid stored URL is recorded as a failed attempt without any network
activity. The fake fetcher performs no validation or network access. It is
only for tests and offline runs.

**Residual limitations (not guaranteed).**

- DNS resolution uses the OS resolver (`socket.getaddrinfo`), which cannot
  be cancelled. The fetch stops waiting at the DNS timeout, but the worker
  thread stays busy until the OS resolver returns. At most 8 such lookups
  run per fetcher; once all workers are stuck, new lookups queue and time
  out (fail closed) instead of spawning more threads.
- Pinning depends on httpcore honouring the `sni_hostname` request
  extension (verified for httpcore 1.0.x). An HTTP client upgrade must keep
  that behaviour. A manual check confirmed that wrong-host, self-signed,
  and expired certificates are still rejected through the pinned path.
- The address policy relies on Python's `ipaddress` registry data, which
  varies slightly across Python versions. Networks that route public
  address space internally (for example, a corporate network announcing
  public prefixes) are not detected.
- Decompression is not requested (`identity`), but a server may still send
  compressed content. Size limits apply to decoded bytes, read chunk by
  chunk, so one compressed chunk can expand in memory before the limit
  check.
- Only ports 80/443 are allowed. Sites on other ports are recorded as
  `disallowed_port` by design.
- Certificate verification uses HTTPX's default CA bundle.

### Qualification and saved prospects (P3)

**Workflow.** Discover → Research → Enrich → **Qualify → Save → Search**.
`POST /companies/{id}/qualify` loads the candidate's enrichment (discovery
fields plus latest usable research, via `get_enrichment`), applies the
rules, and appends a run. It makes no network requests; research the
website first for stronger evidence. `GET /companies/{id}/qualifications`
returns exactly `candidate_id`, `total` (all stored runs), `limit` (the
effective page size, 1–100, default 20), `latest` (`history[0]` or
`null`), and `history` (at most `limit` runs, newest first). `POST /companies/{id}/save` saves a qualified
candidate. `GET /prospects` searches saved prospects.

**Modules.**

| Module | Responsibility |
| --- | --- |
| `app/schemas/qualification.py` | `QualificationCriteria` (validated profile, defaults), factor and record schemas. |
| `app/services/qualification_rules.py` | Pure, versioned rule engine (`evaluate`). No DB, network, or clock. |
| `app/services/qualification.py` | Runs and persists qualifications; history; eligibility; serialization. |
| `app/services/prospects.py` | Eligibility-gated saving, duplicate handling, search, retrieval. |
| `app/routers/qualification.py`, `app/routers/prospects.py` | Thin routes. |
| `app/routers/params.py` | Shared validated path parameters (`CandidateId`, `ProspectId`). |

**Criteria (target-market profile).** Sent in the request body (`criteria`,
optional) and validated by `QualificationCriteria`. Unknown fields are
rejected, and terms are normalized (NFKC, case-folded, whitespace
collapsed), de-duplicated, and sorted, so equivalent profiles are stored
identically.

| Field | Default | Validation |
| --- | --- | --- |
| `profile` | `default-b2b-software` | 1–60 chars, `[A-Za-z0-9 ._-]` |
| `target_keywords` | `b2b`, `platform`, `saas`, `software` | 1–25 terms, 2–60 chars each |
| `target_locations` | none (factor not applicable) | 0–25 terms |
| `target_tlds` | none | final domain label, e.g. `de`, `io` |
| `excluded_keywords` | none (factor not applicable) | 0–25 terms |
| `threshold` | 60 | 1–100 |
| `min_evidence_coverage` | 0.5 | > 0 and ≤ 1 |

Terms allow letters, digits, spaces, and `- . & + / '`. Excluded keywords
must not repeat a target keyword or location (422), since such a profile
contradicts itself. Weights are fixed by the rule version and cannot be
supplied by callers.

**Rules `qualification-rules/v2`.** Every piece of evidence has a level:

| Level | Sources | Meaning |
| --- | --- | --- |
| `website` | `research.page_title`, `research.extracted_text` (latest usable research, complete or partial) | the company's own pages |
| `domain` | `candidate.domain` (TLD checks only) | the company's domain name |
| `discovery` | `candidate.name` (search result title), `candidate.description` (snippet) | third-party search data; often listicles or directories such as "Best Software Companies in Berlin", so only **preliminary** |

Each term is looked up on the website first, then in discovery data.
Matching is case-insensitive and whole-word; multi-word terms match across
any whitespace.

| Factor | Weight | matched | not_matched | unknown |
| --- | --- | --- | --- | --- |
| `industry_fit` (**required**) | 50 | a target keyword on the website; or, while research is missing or partial, in discovery data (`discovery` level) | nothing counts and the research page is complete | nothing found; research missing or partial |
| `location_fit` | 30 | a target location on the website, the domain ends in a target TLD (`domain` level), or, while research is missing or partial, a location in discovery data | nothing counts and the research page is complete | nothing found; research missing or partial |
| `exclusion_check` | 20 | nothing found and the research page is complete | an excluded term is found anywhere, including discovery data (**disqualifying**; conservative by design) | nothing found; research missing or partial |

Once a **complete** (`success`) page was read, terms found only in
discovery data are reported in `signals_unconfirmed` (their evidence has
`counted: false`) and do not score. The company's own website did not
mention them, so the search title is presumed to describe something else.
A factor without criteria is `not_applicable`. **Missing data is never
treated as proof:** absence counts only when a complete page was read; a
snippet or a partial page can support a match but never an absence.

- `score` = round-half-up(100 × points ÷ *evaluated* weight), where the
  evaluated factors are those that are `matched` or `not_matched`. It is 0
  when nothing could be evaluated. Unknown factors never lower the score.
- `evidence_coverage` = evaluated weight ÷ applicable weight.
- `status`, decided in this order:
  1. `unqualified` if a factor is disqualifying;
  2. `insufficient_evidence` if coverage < `min_evidence_coverage`;
  3. `unqualified` if score < `threshold`;
  4. `unqualified` if industry fit is `not_matched` (required factor);
  5. `insufficient_evidence` if industry fit is `unknown`;
  6. `preliminary` if industry fit is matched only at `discovery` level;
  7. otherwise `qualified`.

So **`qualified` requires the industry fit to be confirmed by the company's
own website**: a complete page, or a partial page that contains the term.
A candidate whose fit rests only on search-result text is `preliminary`.
That is a promising lead that cannot be saved until website research
confirms it. A candidate with no website or no research is not penalized: it is
`preliminary` or `insufficient_evidence`, never `unqualified` for missing
data. Because discovery-only matches count only without a complete page,
and only a complete page can produce `not_matched`, a `preliminary` result
always scores 100.

Each factor records its result, points, `evidence_level` (strongest counted
level), `signals_found`, `signals_not_found`, `signals_unconfirmed`, an
explanation, and evidence items. Each evidence item has the signal, source
field, `level`, `counted`, source URL, research ID, and an excerpt of at
most ~160 characters. The `rationale` is a deterministic text summary: one
line for the decision, one per factor including its evidence level.
Within a factor, each distinct term yields at most one evidence item (the
first source in the fixed order) and appears in exactly one of
`signals_found`, `signals_unconfirmed`, or `signals_not_found`, in
criteria order, however many sources contain it. Response-level
`matched_signals`, `unconfirmed_signals`, `missing_signals`, and
`disqualifying_signals` aggregate the factors in factor-then-criteria
order. Each term is reported once, in the strongest list that applies
(matched, then unconfirmed, then missing). Identical inputs and criteria
always give identical results.

*Changes from `qualification-rules/v1`:* v1 read sources in the order name
→ snippet → website, counted a search-title match as full evidence, and
marked such candidates `qualified`, so a listicle title alone could
qualify a company. v2 adds evidence levels, website-first lookup, and
unconfirmed discovery matches. It also makes industry fit required and adds
the `preliminary` status. Weights, matching, the score formula, and
exclusion semantics are unchanged. v1 runs stay in history and readable,
but only v2 runs can justify a new save.

**Qualification model (`company_qualifications`).** Append-only, never
updated or pruned. Columns: `id`, `candidate_id` (FK, `ON DELETE CASCADE`),
`research_id` (FK to the research used, `ON DELETE SET NULL` so research
pruning keeps the run and its evidence excerpts), `rule_version`,
`criteria` (JSONB, canonical), `status`, `score`, `threshold`,
`evidence_coverage`, `factors` (JSONB), `rationale`, `qualified_at`,
`created_at`, `updated_at`. Check constraints cover the status set
(`preliminary` added by migration `0004`), score 0–100, threshold 1–100,
and coverage 0–1. Index: `(candidate_id, qualified_at)`.
`matched_signals`, `missing_signals`, and `disqualifying_signals` in
responses are derived from `factors` rather than stored twice. Stored
criteria are returned through the lenient `StoredCriteria` schema, not
re-validated, so runs recorded under older validation rules stay
readable.

**Saved prospect model (`saved_prospects`).** `id`, `candidate_id` (FK,
`ON DELETE CASCADE`, **unique**), `qualification_id` (FK, explicitly
`ON DELETE NO ACTION`, so the justifying run cannot be deleted on its
own), `saved_at`, and `updated_at`. `NO ACTION` rather than `RESTRICT`:
PostgreSQL checks `NO ACTION` at the end of the statement, so deleting
the candidate can cascade to both the prospect and its qualification,
whereas `RESTRICT` is checked immediately and could block that cascade.
Candidate and research data are not copied.

**Saving rules** (enforced in `app/services/prospects.py`):

1. The candidate must exist (404 `candidate_not_found`).
2. If it is already saved, the existing prospect is returned unchanged
   with HTTP 200 and `created: false`. A later save never swaps the
   justifying qualification.
3. Otherwise the qualification is the one given by `qualification_id`
   (which must belong to the candidate, else 404
   `qualification_not_found`) or the candidate's latest run (none: 409
   `qualification_required`).
4. It must come from a saveable rule version (currently only
   `qualification-rules/v2`), have status `qualified` (`preliminary` is
   refused with a hint to research the website), and have a score at or
   above its recorded threshold (else 409 `prospect_not_qualified`).
5. The insert uses `ON CONFLICT (candidate_id) DO NOTHING`. If a
   concurrent request won, its row is returned with `created: false`, and
   the unique constraint guarantees there are no duplicates.

**Search (`GET /prospects`).** The validated query model rejects unknown
parameters. It supports:

- `q`: 2–100 chars, case-insensitive substring on name, domain, or
  description, with `%`, `_`, and `\` escaped.
- `domain`: exact.
- `min_score` / `max_score`: 0–100, on the justifying qualification.
- `saved_after` (inclusive) / `saved_before` (exclusive): timezone-aware
  ISO 8601.
- `sort`: `saved_at_desc` (default), `saved_at_asc`, `score_desc`, or
  `name_asc`. Ties are always broken by prospect ID.
- `limit`: 1–100 (default 20). `offset`: 0–10000.

The response includes `total`. `GET /prospects/{id}` returns the
candidate, with its discovery provenance, and the full justifying
qualification.

**Error codes.** `candidate_not_found`, `qualification_not_found`,
`prospect_not_found` (404); `qualification_required`,
`prospect_not_qualified` (409); `validation_error` (422);
`qualification_persistence_failed`, `prospect_persistence_failed` (500).

### Outreach Agent

1. Campaigns select saved prospects.
2. Draft generation calls a `DraftGenerator` protocol; the Mistral adapter
   implements it, and prompts are versioned code, not database state.
3. **Approval gate** — drafts are stored in `pending` state. The backend
   refuses to send anything that was not explicitly approved by a recorded
   human action. This is enforced in the service layer, not only in the UI.
4. Sending goes through an `EmailProvider` protocol. Sending is idempotent
   per draft and audited (attempt, result, timestamp).

### Mistral

Accessed only through an `AIProvider` adapter in `app/services/ai/` (or a
dedicated provider package when Phase 1 lands). The app depends on the
adapter's protocol, never on a vendor SDK at the call site. API keys come
from settings, are never logged, and the test suite supplies a fake adapter
so no credentials are required to run tests.

### Database

PostgreSQL via SQLAlchemy 2.x and psycopg 3. Alembic is the only schema
migration mechanism; `Base.metadata` uses a naming convention so constraint
names are deterministic. Tests that need a database use a dedicated test
database (`TEST_DATABASE_URL`, for example `explee_test`) and are skipped
when it is unset; the application database is never touched by tests.

### Email

Sending is an explicit, approval-gated side effect behind an
`EmailProvider` protocol. Dry-run and manual-send paths are expected to be
first-class, so the same workflow can be exercised without delivering mail.

## Non-goals

- Multi-tenancy, authentication, and user management (single personal user).
- Background workers or queues in the near term; FastAPI's lifespan and
  synchronous request handling are sufficient until a real need appears.
- Containers. Deployment targets a local Windows machine with a virtual
  environment and a local PostgreSQL service.

## Phase 0 decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| App construction | Factory `create_app(settings)` | Lets tests inject settings; no import-time env coupling. |
| Settings source | `pydantic-settings`, `.env` at project root | Explicit, typed, validated at startup. |
| DB driver | psycopg 3 (`postgresql+psycopg`) | Current SQLAlchemy 2 recommendation; no libpq build needed. |
| Session scope | Per request via dependency | Predictable lifetime; no session sharing across requests. |
| Health check | Liveness only | Keeps `/health` fast and independent of PostgreSQL; readiness checks arrive with the features that need them. |
| Alembic URL | From app settings | One source of truth; no credentials in `alembic.ini`. |
| Error contract | Single envelope with stable codes | Frontend and tests can branch on `code`, not prose. |
