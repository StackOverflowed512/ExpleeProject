# Architecture

Phase 0 established the skeleton; Phases 1 and 2 add discovery, research, and
enrichment. The target remains a **modular monolith**: one FastAPI process,
one PostgreSQL database, and internal module boundaries that can later be
extracted or replaced without touching business logic.

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

1. **Discovery** — implemented in P1. A `SearchProvider` protocol
   (`app/services/providers/search.py`) accepts a natural-language query and
   a bounded limit and returns normalized `SearchResultItem` records. Two
   adapters exist: `brave` (real API) and `fake` (deterministic, used by
   tests and the credential-free default). Selection happens through
   `SEARCH_PROVIDER`; discovery code never imports a vendor SDK or reads a
   vendor payload.
2. **Research** — implemented in P2. A `WebsiteFetcher` protocol
   (`app/services/providers/website.py`) returns a normalized, bounded
   `WebsiteFetchResult`. One adapter fetches over HTTPX with SSRF checks and
   HTML extraction; a deterministic fake serves tests. Research records are
   append-only and keep their source URL and fetch time.
3. **Enrichment** — implemented in P2. A read-only projection combines the
   discovery fields with stored research and reports a status; it never
   fetches, so it is idempotent and causes no extra network traffic.
4. **Qualification** — deterministic, rule-based scoring in a later phase;
   Mistral-assisted qualification after that, with the score and rationale
   persisted so a decision can be audited.
5. **Persistence** — `company_candidates` stores discovered companies
   through the discovery service, which enforces deduplication and source
   provenance.

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
  `provider_authentication_failed` (502), `provider_unavailable` (502),
  `provider_rate_limited` (503), `provider_timeout` (504), and
  `discovery_persistence_failed` (500). Messages never contain keys or
  provider payloads.

### Research details (P2)

- **Fetch contract.** `WebsiteFetcher.fetch(url)` returns a frozen
  `WebsiteFetchResult` with requested and final URL, status, content type,
  page title, extracted text, `fetched_at`, an `outcome`
  (`success`/`partial`/`failed`), an optional stable `failure_category`, and a
  `truncated` flag. Raw headers, cookies, response bodies, and exception text
  are never part of the contract or the API responses.
- **Only stored URLs.** The API accepts no URL from clients. Research uses
  `company_candidates.website_url`; a candidate without a website returns
  `candidate_website_missing` (422). There is no general crawler and no
  automatic discovery of linked pages.
- **SSRF protection.** `app/services/providers/url_safety.py` validates every
  URL before it is requested and again for every redirect. It accepts only
  `http`/`https` on the default ports, rejects embedded credentials, control
  characters, fragments, ambiguous numeric hosts, known metadata hostnames,
  and non-canonical ports, then resolves the hostname itself and rejects the
  host unless *every* resolved address is a globally routable unicast address
  (`ipaddress.is_global` plus explicit loopback/link-local/private/reserved/
  multicast/unspecified checks and a small metadata-IP denylist). IPv4-mapped
  IPv6 addresses are canonicalized before the check.
- **DNS pinning.** The adapter connects to a validated IP address while
  preserving the logical `Host` header and TLS SNI, which closes the
  resolution-to-connection gap for the fetched request. Redirects are followed
  manually so each hop is revalidated.
- **Documented limitation.** Address validation uses the operating system
  resolver, which is not independently timed and can itself be pointed at a
  private address; those addresses are rejected after resolution, but name
  resolution time is not separately bounded. The fetcher also cannot guarantee
  that a *server-side* proxy or redirect chain never changes behaviour between
  validation and connection beyond the pinned address it chose. TLS
  certificate validation is delegated to HTTPX. These limitations are why the
  fetcher fetches exactly one page per candidate and never follows links.
- **Bounded work.** Explicit connect/read timeouts plus an overall deadline,
  a maximum downloaded byte count, a redirect limit, and a maximum extracted
  character count. Oversized responses, timeouts, connection errors, HTTP
  errors, unsupported content types, and malformed markup all produce typed
  failures rather than exceptions. Only `text/html` and
  `application/xhtml+xml` are processed; visible text excludes scripts,
  styles, navigation, headers, footers, sidebars, forms, and hidden elements.
- **Persistence policy.** `research_records` is append-only. A failed refresh
  never removes an earlier success; changed content or a changed outcome adds
  a record. `attempt_key` chains an attempt to the previous record and a
  result signature, and a unique `(company_candidate_id, attempt_key)`
  constraint makes the same transition safe under concurrency. An identical
  consecutive attempt returns the existing record with `created: false`.
  Original candidate provenance (`source_url`, first-seen identity) is never
  modified.
- **Enrichment.** `app/services/enrichment.py` projects discovery data plus
  the latest successful research. Status is `not_researched`, `researched`,
  `partial`, `stale` (a later attempt failed but earlier research remains), or
  `failed`. The projection is pure, so it is testable without HTTP or a
  database and is idempotent for the same inputs.
- **Error mapping.** `candidate_not_found` (404),
  `candidate_website_missing` (422), and `research_persistence_failed` (500).
  A failed fetch is a stored, auditable result returned with HTTP 200 and a
  `failure_category`, not a server error.

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

## Phase 2 decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| HTTP client | One shared `httpx.Client` per fetcher, closed at app shutdown | Connection reuse and explicit lifecycle; tests inject mock transports. |
| Redirects | Followed manually with per-hop validation | Never trust a `Location` header; enforce a redirect limit and detect loops. |
| DNS | Resolve, validate every address, connect to a chosen IP | Removes the TOCTOU gap the HTTP client would otherwise create. |
| HTML parsing | BeautifulSoup with the standard-library `html.parser` | Reliable extraction without a compiler-heavy dependency. |
| Unsupported types | Fail with `unsupported_content_type` | Avoid persisting binary or non-page content as facts. |
| Research history | Append-only records plus a chained `attempt_key` | Auditable failures without duplicates or lost successes. |
| Enrichment | Pure read-only projection | Idempotent, no hidden network calls, trivially unit testable. |
| Failed research HTTP status | `200` with a typed outcome | The failure is the persisted result, not a transport error. |
