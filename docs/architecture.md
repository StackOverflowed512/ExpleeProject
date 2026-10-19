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

1. **Discovery** — implemented in P1. A `SearchProvider` protocol
   (`app/services/providers/search.py`) accepts a natural-language query and
   a bounded limit and returns normalized `SearchResultItem` records. Two
   adapters exist: `brave` (real API) and `fake` (deterministic, used by
   tests and the credential-free default). Selection happens through
   `SEARCH_PROVIDER`; discovery code never imports a vendor SDK or reads a
   vendor payload.
2. **Research** — fetch and extract public website content through a
   `WebsiteFetcher` protocol; store extracted facts with their source URL.
   Not implemented yet.
3. **Enrichment** — combine discovery and research into a prospect record.
   Not implemented yet.
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
