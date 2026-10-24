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
4. **Qualification** — deterministic, rule-based scoring in a later phase;
   Mistral-assisted qualification after that, with the score and rationale
   persisted so a decision can be audited.
5. **Persistence** — `company_candidates` stores discovered companies
   through the discovery service, which enforces deduplication and source
   provenance. `company_research` stores research attempts per candidate.

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
