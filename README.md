# Explee

Personal AI-powered sales intelligence and outreach platform. Two modules:

- **Explorer** — discover companies, research their websites, enrich and
  qualify prospects, and save them.
- **Outreach Agent** — build campaigns from saved prospects, generate drafts,
  obtain explicit human approval, and send through a configurable provider.

Implemented so far:

- **Phase 0 — foundation:** application factory, typed configuration, health
  endpoint, shared error envelope, database wiring, Alembic setup, tests.
- **Phase 1 — Explorer discovery:** a replaceable search provider, company
  candidate persistence with deduplication and provenance, and
  `POST /discovery/search`.
- **Phase 2 — Explorer research and enrichment:** a bounded, SSRF-protected
  website fetcher, append-only research records with source URLs and
  timestamps, and research/enrichment endpoints.

Qualification (Phase 3) and the Outreach Agent are not implemented yet.

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
discovery needs it because it persists candidates.

## 4b. Configure the Explorer search provider

Discovery needs a web search provider. Two adapters exist:

| `SEARCH_PROVIDER` | Behavior | Credentials |
| --- | --- | --- |
| `fake` (default) | Returns no results. The app and tests run without keys. | None |
| `brave` | Calls the Brave Search API. | `SEARCH_API_KEY` |

To use the real provider:

1. Create a Brave Search API key at
   <https://api-dashboard.search.brave.com/app/keys>.
2. Add to `.env`:

   ```dotenv
   SEARCH_PROVIDER=brave
   SEARCH_API_KEY=your-key-here
   SEARCH_TIMEOUT_SECONDS=10
   SEARCH_MAX_RETRIES=2
   ```

3. Restart the server. If `SEARCH_PROVIDER=brave` is set without a key, the
   application refuses to start with a clear validation error.

Manual verification of the real adapter (uses one API request):

```powershell
python -c "from app.config import get_settings; from app.services.providers import build_search_provider; p = build_search_provider(get_settings()); [print(r.title, r.url) for r in p.search('b2b saas companies berlin', 3)]"
```

The adapter reads only documented fields (`title`, `url`, `description`),
skips malformed results, retries rate limits, 5xx responses and timeouts
with bounded backoff, and never logs the key or raw payloads. The API
contract is documented at
<https://api-dashboard.search.brave.com/app/documentation/web-search>;
re-check it before relying on the adapter in production.

## 4c. Configure website research

Research needs no API credentials. It fetches only the website URL already
stored on a discovered candidate; there is no endpoint for submitting an
arbitrary URL. The defaults are safe for a personal deployment:

| Setting | Default | Meaning |
| --- | --- | --- |
| `WEBSITE_FETCH_CONNECT_TIMEOUT_SECONDS` | `5` | Connection timeout |
| `WEBSITE_FETCH_READ_TIMEOUT_SECONDS` | `10` | Response read timeout |
| `WEBSITE_FETCH_TOTAL_TIMEOUT_SECONDS` | `15` | Overall fetch deadline, including redirects |
| `WEBSITE_FETCH_MAX_BYTES` | `2000000` | Maximum downloaded bytes |
| `WEBSITE_FETCH_MAX_REDIRECTS` | `3` | Maximum redirects followed |
| `WEBSITE_FETCH_MAX_TEXT_CHARS` | `20000` | Maximum retained visible-text characters |

Only public `http`/`https` destinations on the default ports are fetched.
Loopback, private, link-local, multicast, reserved, and cloud metadata
destinations are rejected before connecting, and every redirect destination
is validated again. See `docs/architecture.md` for the DNS-rebinding
limitation and the exact security model.

## 5. Run database migrations

```powershell
alembic upgrade head
alembic current
```

Alembic reads `DATABASE_URL` from the same settings as the application.
Two revisions exist:

| Revision | Creates |
| --- | --- |
| `0001_company_candidates` | `company_candidates` and its partial unique indexes |
| `0002_research_records` | `research_records`, its foreign key, unique attempt key, and candidate/fetched index |

Verify the schema afterwards:

```powershell
psql -U postgres -h localhost -d explee -c "\d company_candidates"
psql -U postgres -h localhost -d explee -c "\d research_records"
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

With `SEARCH_PROVIDER=brave`, `results` contains persisted candidates with
`name`, `website_url`, `domain`, `description`, `provider`, `source_url`,
and timestamps. Repeating a search does not create duplicates; it reports
`created: 0` and fills only previously missing fields.

## 5c. Research and enrich a discovered company

Once a candidate exists, research fetches its stored website and stores an
append-only record. Use the candidate `id` returned by discovery:

```powershell
# Fetch and store research for candidate 1 (no request body needed).
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/companies/1/research | ConvertTo-Json -Depth 5

# Read the full research history, newest first.
Invoke-RestMethod -Uri http://127.0.0.1:8000/companies/1/research | ConvertTo-Json -Depth 5

# Combine discovery data with stored research (read-only, never fetches).
Invoke-RestMethod -Uri http://127.0.0.1:8000/companies/1/enrichment | ConvertTo-Json -Depth 5
```

Example research response (trimmed):

```json
{
  "id": 1,
  "company_candidate_id": 1,
  "requested_url": "https://acme.com/",
  "final_url": "https://www.acme.com/",
  "fetched_at": "2026-10-20T15:00:00Z",
  "http_status": 200,
  "content_type": "text/html; charset=utf-8",
  "page_title": "Acme Corp",
  "page_text": "Acme builds reliable widgets.",
  "outcome": "success",
  "failure_category": null,
  "truncated": false,
  "created": true
}
```

Policy and status codes:

- `research_records` is append-only. A failed refresh is stored and returned
  as `outcome: "failed"` with a stable `failure_category` (for example
  `timeout`, `http_error`, `unsupported_content_type`, `blocked_destination`,
  or `response_too_large`), but it never overwrites an earlier success.
- Repeating an identical consecutive attempt returns the existing record with
  `created: false`; changed page content or a changed outcome creates a new
  record. `POST /companies/{id}/research` returns `200` for successful,
  partial, and failed fetches because the failure itself is a stored,
  auditable result. Unknown candidates return `404`; candidates without a
  website return `422 candidate_website_missing`.
- `GET /companies/{id}/research` returns every attempt plus
  `latest_successful`. `GET /companies/{id}/enrichment` returns the discovery
  fields, the latest usable research, the latest attempt, and a status of
  `not_researched`, `researched`, `partial`, `stale`, or `failed`.

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

Tests are deterministic, use no network, no live website, no live database,
and no Mistral credentials. Website fetches are exercised with HTTPX mock
transports and injected DNS; research uses the fake fetcher. PostgreSQL-backed
integration tests (discovery persistence, research persistence, API
persistence, migrations, `alembic check`) run only when `TEST_DATABASE_URL`
points at a **disposable** database; otherwise they are skipped:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://explee:change-me@localhost:5432/explee_test"
pytest -q
```

The migration tests drop and recreate the schema in that database (one of them
downgrades to `base` and upgrades back to `head`), so never point
`TEST_DATABASE_URL` at real data. To create the test database in pgAdmin:
**Databases → Create → Database**, name `explee_test`, owner the same role.

Verified on 2026-10-20 with Python 3.14.8 and PostgreSQL 17.5:
`278 passed` with `TEST_DATABASE_URL` set; `216 passed, 62 skipped` without it.
The only warning is a third-party Starlette/httpx deprecation raised from
FastAPI's `testclient.py`.

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
    research_record.py    append-only website research attempts
  schemas/           Pydantic request/response schemas
  routers/           thin HTTP routes (health, discovery, research)
  services/          business logic
    discovery.py          search -> normalize -> dedupe -> persist
    discovery_urls.py     URL/domain normalization
    research.py           fetch -> extract -> persist with provenance
    enrichment.py         discovery + stored research (read-only)
    providers/            SearchProvider and WebsiteFetcher protocols
                          + brave/fake search and http/fake website adapters
alembic/             migration environment (URL from app settings)
docs/                architecture and phased plan
scripts/             operational helpers (check_db)
tests/unit/          fast, isolated tests
tests/integration/   ASGI app tests; DB tests when TEST_DATABASE_URL is set
```

See `docs/architecture.md` for boundaries and `docs/implementation-plan.md`
for the roadmap.
