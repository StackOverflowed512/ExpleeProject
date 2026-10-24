# Explee

Personal AI-powered sales intelligence and outreach platform. Two modules:

- **Explorer** — discover companies, research their websites, enrich and
  qualify prospects, and save them.
- **Outreach Agent** — build campaigns from saved prospects, generate drafts,
  obtain explicit human approval, and send through a configurable provider.

This repository currently contains **Phases 0, 1, 1.1, 2, and 3**:
application skeleton, configuration, health endpoint, error contract,
database wiring, migrations, Explorer company discovery backed by Tavily
Search, website research and enrichment, deterministic qualification, and
saved prospects with search. Outreach features are not implemented yet.

## Prerequisites

- Windows 11 (the project also runs on macOS/Linux, but the instructions
  below target Windows PowerShell).
- Python **3.12 or newer** (`python --version`).
- PostgreSQL **15 or newer** with `psql` on `PATH`.
- Git.

Docker is not used or required.

## 1. Create and activate a virtual environment (PowerShell)

```powershell
cd C:\Users\91798\Desktop\Explee
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python --version
```

`Set-ExecutionPolicy -Scope Process` affects only the current shell session.
If you prefer not to change it, run `.\.venv\Scripts\python.exe` explicitly for
every command instead of activating.

## 2. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

This installs the runtime and test dependencies declared in `pyproject.toml`.
No credentials are needed for installation or tests.

## 3. Create the local environment file

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `DATABASE_URL` to your PostgreSQL credentials. The
application only accepts the psycopg 3 driver, so the scheme must be
`postgresql+psycopg`. `.env` is ignored by Git.

## 4. Configure PostgreSQL on Windows

1. Install PostgreSQL from the official EDB installer
   (<https://www.postgresql.org/download/windows/>). Remember the password
   chosen for the `postgres` superuser and keep port `5432` (the default).
   The installer adds `psql` to `PATH`; open a new terminal afterwards.
2. Create the application role and databases — choose **Option A (pgAdmin)**
   or **Option B (psql)**.

   **Option A — pgAdmin 4 (graphical; installed with PostgreSQL):**

   1. Open **pgAdmin 4** from the Start menu.
   2. In the left tree, expand **Servers → PostgreSQL \<version\>** and enter
      the `postgres` password chosen during installation.
   3. Create the role: right-click **Login/Group Roles → Create → Login/Group
      Role…**
      - **General** tab → **Name**: `explee`
      - **Definition** tab → **Password**: your chosen password
      - **Privileges** tab → **Can login?**: `Yes`
      - Click **Save**.
   4. Create the database: right-click **Databases → Create → Database…**
      - **General** tab → **Database**: `explee`
      - **Owner**: `explee`
      - Click **Save**.
   5. Repeat step 4 with database name `explee_test` (reserved for
      database-backed tests in later phases). The owner automatically has all
      privileges.
   6. Optional check: right-click the `explee` database → **Query Tool** and run
      `SELECT current_database(), current_user;`.

   **Option B — psql:**

   ```powershell
   psql -U postgres -h localhost -c "CREATE ROLE explee WITH LOGIN PASSWORD 'change-me';"
   psql -U postgres -h localhost -c "CREATE DATABASE explee OWNER explee;"
   psql -U postgres -h localhost -c "CREATE DATABASE explee_test OWNER explee;"
   ```

   If the role already exists, skip the first command.

3. Point `.env` at the role:

   ```dotenv
   DATABASE_URL=postgresql+psycopg://explee:change-me@localhost:5432/explee
   ```

   Use the password you set in pgAdmin. Percent-encode reserved characters
   (`@` → `%40`, `:` → `%3A`, `/` → `%2F`, `#` → `%23`, `?` → `%3F`,
   `%` → `%25`, space → `%20`). Simplest is an ASCII-only password without
   these characters. Do not wrap the value in quotes.
4. Verify connectivity:

   ```powershell
   python -m scripts.check_db
   ```

   Expected output names the database and prints the PostgreSQL version. The
   password is never printed.

If PostgreSQL is unavailable, the app still starts and `GET /health` works;
discovery and research need it because they persist candidates and research
attempts.

## 4b. Configure the Explorer search provider

Discovery needs a web search provider. Two adapters exist:

| `SEARCH_PROVIDER` | Behavior | Credentials |
| --- | --- | --- |
| `tavily` (default) | Calls the Tavily Search API. | `TAVILY_API_KEY` |
| `fake` | Returns no results. Used by tests and for credential-free runs. | None |

To use Tavily:

1. Create a Tavily API key at <https://app.tavily.com/home>.
2. Add to `.env`:

   ```dotenv
   SEARCH_PROVIDER=tavily
   TAVILY_API_KEY=tvly-your-key-here
   SEARCH_TIMEOUT_SECONDS=10
   SEARCH_MAX_RETRIES=2
   ```

3. Restart the server. The key is optional at startup, so `/health` and
   unrelated routes work without it. A discovery request made while
   `SEARCH_PROVIDER=tavily` and no key is configured returns the safe error
   envelope `provider_not_configured` (HTTP 503). The app never silently
   switches to the fake provider when Tavily credentials are missing.

Manual verification of the real adapter (uses one API request and therefore
consumes Tavily credits):

```powershell
python -c "from app.config import get_settings; from app.services.providers import build_search_provider; p = build_search_provider(get_settings()); [print(r.title, r.url) for r in p.search('b2b saas companies berlin', 3)]"
```

The adapter posts JSON to `https://api.tavily.com/search` with a Bearer
token, reads only documented fields (`title`, `url`, `content`, optional
`id`), skips malformed individual results, retries only transient failures
(429, 5xx, timeouts, connection errors) with bounded backoff, honors a
bounded `Retry-After`, and never logs the key or raw payloads. Authentication
failures and other permanent 4xx responses are not retried. The API contract
is documented at
<https://docs.tavily.com/documentation/api-reference/endpoint/search>;
re-check it before relying on the adapter in production.

**Free-tier and limitations.** Tavily's free-tier allowance is limited and
subject to Tavily's current pricing and terms; it is not unlimited or
guaranteed to remain free. See <https://www.tavily.com/pricing> for the
current allowance. The adapter requests `search_depth=basic` to keep each
search at the documented base credit cost. Search result titles, snippets,
and URLs are third-party content, not verified facts. Tavily may omit the
optional per-result `id`; discovery then deduplicates on the normalized
domain. Automated tests never call Tavily and require no key; only the manual
command above exercises the live API.

## 5. Run database migrations

```powershell
alembic upgrade head
alembic current
```

Alembic reads `DATABASE_URL` from the same settings as the application.
Revisions:

| Revision | Phase | Creates |
| --- | --- | --- |
| `0001_company_candidates` | P1 | `company_candidates` and its partial unique indexes |
| `0002_company_research` | P2 | `company_research` (FK to candidates, `ON DELETE CASCADE`) |
| `0003_qualification_prospects` | P3 | `company_qualifications` (append-only runs) and `saved_prospects` (unique per candidate) |
| `0004_qualification_preliminary` | P3 | widens the qualification status check to allow `preliminary` (no data changes) |

Existing Phase 1 and Phase 2 databases upgrade in place with
`alembic upgrade head`. Every revision is additive, so existing candidates
and research are untouched. To roll back only Phase 3, run
`alembic downgrade 0002_company_research`. That drops the qualification and
prospect tables **and their data**. Downgrading `0004` alone is refused
while `preliminary` runs exist. Verify the schema afterwards:

```powershell
psql -U postgres -h localhost -d explee -c "\d company_candidates"
psql -U postgres -h localhost -d explee -c "\d company_research"
psql -U postgres -h localhost -d explee -c "\d company_qualifications"
psql -U postgres -h localhost -d explee -c "\d saved_prospects"
```

Useful commands:

```powershell
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic downgrade -1
alembic check
```

## 5b. Try a discovery search

With the server running:

```powershell
$body = @{ query = "B2B SaaS companies hiring sales engineers in Berlin"; limit = 5 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/discovery/search -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 5
```

Example response (`SEARCH_PROVIDER=fake` returns no results):

```json
{
  "query": "B2B SaaS companies hiring sales engineers in Berlin",
  "provider": "fake",
  "requested_limit": 5,
  "results": [],
  "counts": { "returned": 0, "created": 0, "updated": 0, "skipped": 0 }
}
```

With `SEARCH_PROVIDER=tavily` and a configured key, `results` contains
persisted candidates with `name`, `website_url`, `domain`, `description`,
`provider`, `provider_result_id`, `source_url`, and timestamps. Repeating a
search does not create duplicates; it reports `created: 0` and fills only
previously missing fields.

## 5c. Research and enrich a candidate

Research fetches the website **already stored on a discovered candidate**.
It uses direct HTTP (HTTPX), not Tavily, so it consumes no search credits
and needs no API key. No endpoint accepts a URL from the caller.

```powershell
# Request research (reuses fresh research; add -Body '{"refresh": true}' to force a fetch)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/companies/1/research -ContentType "application/json" -Body '{}' | ConvertTo-Json -Depth 5

# Stored research: latest usable record plus retained attempt history
Invoke-RestMethod http://127.0.0.1:8000/companies/1/research | ConvertTo-Json -Depth 5

# Discovery data combined with the latest usable research
Invoke-RestMethod http://127.0.0.1:8000/companies/1/enrichment | ConvertTo-Json -Depth 5
```

| Endpoint | Behaviour |
| --- | --- |
| `POST /companies/{id}/research` | Body `{"refresh": false}` (optional; unknown fields → 422). Returns `status`, `reused`, `attempt`, `latest_usable`. |
| `GET /companies/{id}/research` | Returns `status`, `latest_usable` (with text), `attempts` (metadata only, newest first). Never fetches. |
| `GET /companies/{id}/enrichment` | Returns discovery fields and provenance, `research_status`, `website`, `last_attempt`. Read-only and deterministic. |

`status` is `available`, `partial` (size limit hit), `unavailable` (attempted,
nothing usable), or `not_researched`. Each attempt's `outcome` is `success`,
`partial`, `empty`, or `failed`. A failed fetch is recorded with a safe
`failure_category` (for example `timeout`, `http_error`,
`blocked_destination`) and returns HTTP 200; earlier usable research stays
in `latest_usable`. Errors use the shared envelope: `candidate_not_found`
(404), `candidate_website_missing` (409), `validation_error` (422),
`research_persistence_failed` (500).

Settings (all optional; safe defaults, see `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `WEBSITE_FETCHER` | `httpx` | `httpx` (real) or `fake` (no network) |
| `RESEARCH_CONNECT_TIMEOUT_SECONDS` | `5` | TCP/TLS connect timeout |
| `RESEARCH_READ_TIMEOUT_SECONDS` | `10` | Read/write timeout |
| `RESEARCH_MAX_FETCH_SECONDS` | `20` | Total budget per fetch, redirects included |
| `RESEARCH_MAX_RESPONSE_BYTES` | `2000000` | Body bytes read; more → `partial` |
| `RESEARCH_MAX_REDIRECTS` | `5` | Redirects followed, each revalidated |
| `RESEARCH_MAX_TEXT_CHARS` | `50000` | Stored text cap |
| `RESEARCH_FRESHNESS_HOURS` | `24` | Reuse window; `0` disables reuse |

**Safety.** Fetches go only to public addresses on ports 80/443. URLs with
credentials, non-HTTP schemes, alternate numeric IP forms, and
local/internal names are rejected. Every resolved address must be public.
The connection is pinned to the validated IP (mitigating DNS rebinding),
and every redirect is revalidated. Only HTML is parsed, and page text is
stored as third-party content, not verified facts. Each candidate keeps its
five most recent attempts plus the latest usable one. Full details and
residual limitations are in `docs/architecture.md`.

## 5d. Qualify, save, and search prospects

Qualification applies deterministic, versioned rules
(`qualification-rules/v2`) to the candidate's **stored** discovery data and
latest usable research. It makes no network or AI calls. **Research the
website first**: a match found only in the search result's title or
snippet is preliminary and cannot be saved. Describe your target market in
`criteria`, or omit it to use the defaults (`b2b`, `platform`, `saas`,
`software`; threshold 60).

```powershell
$criteria = @{ criteria = @{ profile = "berlin-saas"; target_keywords = @("saas", "b2b"); target_locations = @("berlin", "germany"); target_tlds = @("de"); excluded_keywords = @("recruitment agency"); threshold = 60 } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/companies/1/qualify -ContentType "application/json" -Body $criteria | ConvertTo-Json -Depth 8

# Qualification history (newest first)
Invoke-RestMethod http://127.0.0.1:8000/companies/1/qualifications | ConvertTo-Json -Depth 8

# Save (uses the latest qualification; 409 unless it is "qualified", e.g. "preliminary")
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/companies/1/save -ContentType "application/json" -Body '{}' | ConvertTo-Json -Depth 5

# Search saved prospects
Invoke-RestMethod "http://127.0.0.1:8000/prospects?q=acme&min_score=60&sort=score_desc&limit=20" | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/prospects/1 | ConvertTo-Json -Depth 8
```

**Scoring.** There are three factors: `industry_fit` (50 points),
`location_fit` (30), and `exclusion_check` (20). A factor you don't
configure is `not_applicable`.

- Evidence has a level: `website` (the company's own pages), `domain`
  (TLD), or `discovery` (the search result's title and snippet, which are
  often listicles such as "Best Software Companies in Berlin"). Terms are
  looked up on the website first.
- A discovery-only match counts while research is missing or partial.
  Once a complete page was read and it doesn't mention the term, the match
  is listed under `signals_unconfirmed` and does not count.
- A factor is **unknown** (rather than failed) when the evidence is
  missing: a term's absence only counts if a complete website page was
  read.
- The score is points ÷ evaluated weight × 100, so missing data never
  lowers it.
- Results:
  - an excluded term found anywhere → `unqualified`;
  - fewer than `min_evidence_coverage` (default 50%) of the factor weight
    evaluable → `insufficient_evidence`;
  - score below threshold, or industry fit not met (it is required) →
    `unqualified`;
  - industry fit not evaluable → `insufficient_evidence`;
  - industry fit supported only by search-result data → `preliminary`;
  - otherwise → `qualified`.

Each run stores its criteria, factors, evidence excerpts with source URLs,
and a plain-text rationale. Runs are never overwritten.

**Saving.** Only a persisted `qualified` run from the current rules
(`qualification-rules/v2`) can justify a save; the backend enforces this.
For a `preliminary` result, run `POST /companies/{id}/research` and
qualify again. Saving twice returns the existing prospect with HTTP
200 and `created: false`, never a duplicate. Errors: `qualification_required`
and `prospect_not_qualified` (409), `qualification_not_found` and
`prospect_not_found` (404).

**Search.** `GET /prospects` takes these optional parameters:

- `q`: text in name, domain, or description.
- `domain`
- `min_score`, `max_score`
- `saved_after`, `saved_before`: ISO 8601 with a timezone.
- `sort`: `saved_at_desc` (default), `saved_at_asc`, `score_desc`, or
  `name_asc`.
- `limit` (1–100), `offset`.

Unknown parameters are rejected with 422.

## 6. Start the development server

```powershell
uvicorn app.main:app --reload
```

- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

Example health response:

```json
{
  "status": "ok",
  "service": "explee",
  "environment": "development",
  "version": "0.1.0"
}
```

`GET /health` is a liveness check: it reports that the process is serving and
deliberately does not query PostgreSQL.

## 7. Run tests

```powershell
pytest
```

To run one layer:

```powershell
pytest tests/unit
pytest tests/integration
```

Tests are deterministic, use no network, no live database, and no Tavily or
Mistral credentials. Tavily and the website fetcher are exercised only
through HTTPX mock transports, injected DNS resolvers, and an HTML fixture in
`tests/fixtures/websites/`. Test settings use `SEARCH_PROVIDER=fake` and
`WEBSITE_FETCHER=fake`. PostgreSQL-backed integration tests (discovery and
research persistence, API persistence, migrations) run only when
`TEST_DATABASE_URL` points at a **disposable** database; otherwise they are
skipped:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://explee:change-me@localhost:5432/explee_test"
pytest -q
```

The migration tests drop the application tables in that database, upgrade
to `head`, and also upgrade from the Phase 1 revision with data present, so
never point `TEST_DATABASE_URL` at real data. To create the
test database in pgAdmin: **Databases → Create → Database**, name
`explee_test`, owner the same role.

## 8. Common setup problems

| Symptom | Cause and fix |
| --- | --- |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again. |
| `uvicorn` or `alembic` not found | The virtual environment is not active, or dependencies were not installed. Activate it and run `pip install -e ".[dev]"`. |
| `ModuleNotFoundError: No module named 'app'` | Run commands from the project root with the virtual environment active, or reinstall with `pip install -e ".[dev]"`. |
| `psql: command not found` | PostgreSQL's `bin` directory is not on `PATH`. Open a new terminal after installing, or add `C:\Program Files\PostgreSQL\<version>\bin` to `PATH`. |
| `connection failed: password authentication failed` | The credentials in `DATABASE_URL` do not match the PostgreSQL role. Recreate the role or fix the URL; percent-encode special characters. |
| `connection refused` on port 5432 | The PostgreSQL service is not running. Start it from Windows Services (`postgresql-x64-<version>`). |
| `DATABASE_URL must use the 'postgresql+psycopg' driver` | Replace the scheme `postgresql://` or `postgresql+psycopg2://` with `postgresql+psycopg://`. |
| `alembic.util.exc.CommandError` with an empty URL | `.env` is missing or `DATABASE_URL` is unset. Copy `.env.example` to `.env`. |
| Port 8000 already in use | Run `uvicorn app.main:app --reload --port 8001`. |

## Project layout

```
app/
  main.py            application factory and entry point
  config.py          Pydantic settings (env + .env)
  database.py        engine, session factory, request-scoped session
  errors.py          error envelope and exception handlers
  logging_config.py  logging setup
  models/            SQLAlchemy models and declarative base
    company_candidate.py  discovered company candidates
    company_research.py   website research attempts per candidate
    company_qualification.py  append-only qualification runs
    saved_prospect.py     saved prospects (one per candidate)
  schemas/           Pydantic request/response schemas
  routers/           thin HTTP routes (health, discovery, research,
                     qualification, prospects)
  services/          business logic
    discovery.py          search -> normalize -> dedupe -> persist
    discovery_urls.py     URL/domain normalization
    research.py           fetch stored website -> persist attempt -> prune
    enrichment.py         read-only discovery + research projection
    qualification_rules.py  pure, versioned scoring rules
    qualification.py      run + persist qualification, history
    prospects.py          eligibility-gated save, search, retrieval
    providers/            SearchProvider (tavily/fake) and WebsiteFetcher
                          (httpx/fake) adapters, SSRF checks, HTML text
alembic/             migration environment (URL from app settings)
docs/                architecture and phased plan
scripts/             operational helpers (check_db)
tests/unit/          fast, isolated tests
tests/integration/   ASGI app tests; DB tests when TEST_DATABASE_URL is set
tests/fixtures/      recorded HTML fixtures (no live websites in tests)
```

See `docs/architecture.md` for boundaries and `docs/implementation-plan.md`
for the roadmap.
