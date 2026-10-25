# Explee

Personal AI-powered sales intelligence and outreach platform. Two modules:

- **Explorer** — discover companies, research their websites, enrich and
  qualify prospects, and save them.
- **Outreach Agent** — build campaigns from saved prospects, generate drafts,
  obtain explicit human approval, and send through a configurable provider.

This repository currently contains **Phases 0 through 6**:
application skeleton, configuration, health endpoint, error contract,
database wiring, migrations, Explorer company discovery backed by Tavily
Search, website research and enrichment, deterministic qualification,
saved prospects with search, outreach campaigns with Mistral-generated
drafts, an explicit human approval gate with audited, single-draft
email delivery (SMTP or a fake provider, dry-run by default), and a
server-rendered web interface (Jinja2, HTMX, Tailwind CSS) at `/ui/`.
There is no authentication: run it locally for one person only.

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
No credentials are needed for installation or tests. Phase 6 added Jinja2
for the web interface, so an existing environment must run
`pip install -e ".[dev]"` again.

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
| `0005_campaigns_drafts` | P4 | `campaigns`, `campaign_prospects` (unique per campaign), and `outreach_drafts` (`pending` only) |
| `0006_check_constraint_names` | P4 follow-up | renames the CHECK constraints from `0002`–`0004` to the model names (no data or rule changes) |
| `0007_approval_delivery` | P5 | `draft_approvals`, `email_send_attempts`, and the draft delivery states (existing drafts stay `pending`) |

Existing Phase 1 and Phase 2 databases upgrade in place with
`alembic upgrade head`. Every revision is additive, so existing candidates
and research are untouched. To roll back only Phase 3, run
`alembic downgrade 0002_company_research`. That drops the qualification and
prospect tables **and their data**. Downgrading `0004` alone is refused
while `preliminary` runs exist. To roll back only Phase 4, run
`alembic downgrade 0004_qualification_preliminary`, which drops the
campaign and draft tables **and their data**. Verify the schema afterwards:

```powershell
psql -U postgres -h localhost -d explee -c "\d company_candidates"
psql -U postgres -h localhost -d explee -c "\d company_research"
psql -U postgres -h localhost -d explee -c "\d company_qualifications"
psql -U postgres -h localhost -d explee -c "\d saved_prospects"
psql -U postgres -h localhost -d explee -c "\d outreach_drafts"
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

# Qualification history (newest first; limit 1-100, default 20)
Invoke-RestMethod "http://127.0.0.1:8000/companies/1/qualifications?limit=5" | ConvertTo-Json -Depth 8

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
and a plain-text rationale. Runs are never overwritten. A qualification
record also lists `matched_signals`, `unconfirmed_signals` (found only in
search-result data and refuted by the complete website page),
`missing_signals`, and `disqualifying_signals`. Each term appears once, in
the first list that applies.

The history endpoint returns:

```json
{
  "candidate_id": 1,
  "total": 3,
  "limit": 5,
  "latest": { "id": 3, "status": "qualified", "...": "..." },
  "history": [ { "id": 3 }, { "id": 2 }, { "id": 1 } ]
}
```

`total` counts every stored run; `limit` is the effective page size;
`history` holds at most `limit` runs, newest first; `latest` is
`history[0]` (or `null`).

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

## 5e. Create campaigns and generate outreach drafts

Phase 4 turns saved prospects into **pending** email drafts for later human
review. Generation never approves or sends anything; approval and delivery
are separate, explicit steps (see 5f).

**Configure the draft generator** in `.env` (see `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DRAFT_GENERATOR` | `mistral` | `mistral` (real API) or `fake` (deterministic, no network) |
| `MISTRAL_API_KEY` | none | Required only for real generation; create one at <https://console.mistral.ai/> |
| `MISTRAL_MODEL` | `mistral-small-latest` | Any Mistral chat model ID |
| `MISTRAL_TIMEOUT_SECONDS` | `30` | Per-request timeout (0–120) |
| `MISTRAL_MAX_RETRIES` | `1` | Retries for 429/5xx/timeouts (0–3) |

The key is optional at startup. With `DRAFT_GENERATOR=mistral` and no key,
draft generation returns `provider_not_configured` (HTTP 503) and saves
nothing. For credential-free runs set `DRAFT_GENERATOR=fake`. Each draft
costs one Mistral API call; check Mistral's current pricing and limits.

```powershell
# Create a campaign from saved prospect IDs (order is kept)
$campaign = @{
  name = "Q4 fleet operators"
  product = "RouteWise"
  value_proposition = "RouteWise cuts dispatch planning time with live route suggestions."
  target_audience = "Operations leaders at logistics software companies"
  call_to_action = "Would a 20-minute call next week be useful?"
  tone = "professional"
  sender_name = "Alex"
  sender_company = "RouteWise"
  prospect_ids = @(1, 2)
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/campaigns -ContentType "application/json" -Body $campaign | ConvertTo-Json -Depth 6

# List campaigns and read one
Invoke-RestMethod "http://127.0.0.1:8000/campaigns?limit=20" | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:8000/campaigns/1 | ConvertTo-Json -Depth 6

# Generate drafts (skips prospects that already have one; up to 10 per call)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/campaigns/1/drafts -ContentType "application/json" -Body '{}' | ConvertTo-Json -Depth 6

# Add a new revision for one prospect (earlier drafts are kept)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/campaigns/1/drafts -ContentType "application/json" -Body '{"saved_prospect_ids": [2], "regenerate": true}' | ConvertTo-Json -Depth 6

# List a campaign's drafts and read one
Invoke-RestMethod "http://127.0.0.1:8000/campaigns/1/drafts?limit=20" | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8000/drafts/1 | ConvertTo-Json -Depth 6
```

| Endpoint | Behaviour |
| --- | --- |
| `POST /campaigns` | 201 with the campaign and its prospects. IDs must be existing **saved prospects** (422 `invalid_prospect_ids`); duplicates and unknown fields are rejected (422). |
| `GET /campaigns` | Newest first; `limit` 1–100, `offset`, `total`. |
| `GET /campaigns/{id}` | Configuration plus prospects in selection order. |
| `POST /campaigns/{id}/drafts` | Body `{"saved_prospect_ids"?: [...], "regenerate"?: false}`. Returns `results` (`generated` or `existing` per prospect), `batch_limit` (10), and `remaining`. |
| `GET /campaigns/{id}/drafts` | Campaign order, then revision; `limit`, `offset`, optional `saved_prospect_id`. |
| `GET /drafts/{id}` | One draft with its audit trail. |

**How drafts are generated.** The prompt (`outreach-draft/v1`) is built
deterministically from the campaign and the prospect's stored data:

- company name and domain;
- the search snippet;
- the website title and opening text;
- matched market terms and website evidence from the qualification.

Prospect data is treated as untrusted data, not instructions. The model is
told never to invent people, contact details, numbers, customers,
integrations, or relationships, and to write a generic message when data is
thin. Every answer must be JSON with a subject and body. It's validated
before anything is saved and rejected (`provider_invalid_output`, 502) if
it's empty, malformed, truncated, or contains an email address or a
placeholder such as `[Name]`.

**Repeat and failure behaviour.**

- Each draft is saved in its own transaction only after validation, so a
  provider failure never leaves a partial draft.
- If the provider fails partway, the error envelope lists
  `generated_draft_ids` and `failed_saved_prospect_id`. Calling again skips
  existing drafts and continues where it stopped.
- `regenerate: true` adds a new revision instead of overwriting.
- Provider errors: `provider_not_configured` (503),
  `provider_rate_limited` (503), `provider_timeout` (504), and
  `provider_authentication_failed`, `provider_unavailable` and
  `provider_invalid_output` (502).

Each draft records its prompt version, a SHA-256 of the exact prompt,
provider, model, safe response metadata (response ID, finish reason, token
usage), and provenance: candidate, qualification, research, and the
evidence sources used.

## 5f. Approve and send drafts (Phase 5)

Every email needs an explicit, recorded approval of one exact draft
revision for one recipient that **you** supply; recipients are never
guessed. Sending then handles exactly one draft per request and audits
every attempt. The app has **no authentication**: an approval is an
explicit action in this local single-user workflow, recorded with the
`APPROVAL_ACTOR` label. It does not prove anyone's identity.

**Safe by default.** `EMAIL_PROVIDER=fake` and `EMAIL_DRY_RUN=true`, so a
fresh setup can only record dry runs, and no email can leave the machine.
The fake provider never delivers: `"mode": "deliver"` is refused with 409
`delivery_disabled` unless `EMAIL_PROVIDER=smtp`, even with
`EMAIL_DRY_RUN=false`, so a draft can never be marked `sent` by mistake.

```powershell
# 1. Review the draft, then approve that exact revision for a recipient you choose
Invoke-RestMethod http://127.0.0.1:8000/drafts/1 | ConvertTo-Json -Depth 6
$approve = @{ recipient_email = "ops@acme.com"; revision = 1 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/drafts/1/approve -ContentType "application/json" -Body $approve | ConvertTo-Json -Depth 6

# 2. Dry run: records an attempt, contacts no mail server, changes nothing
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/drafts/1/send -ContentType "application/json" -Body '{"mode": "dry_run"}' | ConvertTo-Json -Depth 6

# 3. Real delivery (only after the SMTP setup below)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/drafts/1/send -ContentType "application/json" -Body '{"mode": "deliver"}' | ConvertTo-Json -Depth 6
```

| Endpoint | Behaviour |
| --- | --- |
| `GET /drafts/{id}` | Draft plus `approval` and all `send_attempts` (oldest first). |
| `POST /drafts/{id}/approve` | `{"recipient_email", "revision"}`. 201 new; 200 identical repeat; 409 `draft_already_approved` for a different recipient (generate a new revision instead), `draft_revision_mismatch`, `invalid_draft_state`. |
| `POST /drafts/{id}/send` | `{"mode": "dry_run" \| "deliver"}` only, never a recipient or content. Returns `dry_run`, `sent`, or `already_sent` (never sends twice). |
| `POST /drafts/{id}/resolve-delivery` | `{"delivered": true \| false, "note"?}`. Records a verified outcome after an uncertain or interrupted send. |

Draft states: `pending` → `approved` → `sending` → `sent`. A definite
failure returns the draft to `approved`, where retrying is safe. An
uncertain outcome leaves it in `delivery_uncertain`. Only one revision per
campaign prospect can ever be sent.

**Errors.**

- 409: `draft_not_approved`, `approval_content_mismatch` (the draft or
  approval changed), `delivery_disabled` (dry-run only, or the fake provider), `delivery_in_progress`,
  `delivery_outcome_uncertain`, `prospect_already_emailed`.
- 503: `email_not_configured`.
- Provider errors: `email_connection_failed`, `email_tls_failed`,
  `email_authentication_failed`, `email_rejected` (502), `email_timeout`
  (504), and `email_delivery_uncertain` (502). The error `details` include
  `attempt_id` and `retry_safe`.

**If a send is uncertain** (`email_delivery_uncertain`, or a draft stuck in
`sending` for more than 15 minutes because the server process stopped
mid-send), the message **may have been delivered** and is never retried
automatically. While a send is still running, however slowly,
`resolve-delivery` returns 409 `delivery_in_progress`. If the server is
stuck, stop it first.

1. Find the attempt's `message_id` in `GET /drafts/{id}`.
2. Look for it in your Sent folder or your mail provider's logs.
3. Record the result:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/drafts/1/resolve-delivery -ContentType "application/json" -Body '{"delivered": true, "note": "Found in Sent folder"}' | ConvertTo-Json -Depth 6
```

`delivered: false` returns the draft to `approved` so it can be sent again.

**Configure real SMTP delivery** in `.env` (placeholders in `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMAIL_PROVIDER` | `fake` | `smtp` for real delivery |
| `EMAIL_DRY_RUN` | `true` | must be `false` before `"mode": "deliver"` is allowed |
| `EMAIL_SENDER` | none | From address (required) |
| `SMTP_HOST` | none | SMTP server hostname |
| `SMTP_PORT` | `587` | usually 587 (STARTTLS) or 465 (implicit TLS) |
| `SMTP_SECURITY` | `starttls` | `starttls` or `ssl`; certificates are always verified, and plaintext is not supported |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | none | set both or neither |
| `SMTP_TIMEOUT_SECONDS` | `20` | per connection and command |
| `APPROVAL_ACTOR` | `local-operator` | label recorded on approvals |

**Testing real delivery safely (manual, never automated):**

1. Use a mailbox you own as the recipient, and an SMTP account or
   sandboxed relay (for example a provider's test or sandbox mode) as the
   sender.
2. Set `EMAIL_PROVIDER=smtp`, `EMAIL_SENDER` and the `SMTP_*` settings, and
   keep `EMAIL_DRY_RUN=true`. Restart the server and dry-run one approved
   draft.
3. Set `EMAIL_DRY_RUN=false`, restart, and send **one** approved draft with
   `"mode": "deliver"`. Confirm it arrives, and that `GET /drafts/{id}`
   shows a `sent` attempt with the same `message_id`.
4. Set `EMAIL_DRY_RUN=true` again when you're done.

Never commit `.env`. Automated tests use only the fake provider or a mocked
SMTP client, and require no SMTP credentials.

## 6. Start the development server

```powershell
uvicorn app.main:app --reload
```

- Web interface: <http://127.0.0.1:8000/ui/>
- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

`uvicorn` listens on `127.0.0.1` by default. Do not pass `--host 0.0.0.0`:
the app has no authentication.

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

## 6b. Use the web interface (Phase 6)

With the server running, open **<http://127.0.0.1:8000/ui/>**. The
interface is served by the same process as the API. It needs no Node.js and
no build step to run; the compiled stylesheet and HTMX are committed.

> **Local, single-user tool.** There is no sign-in. Keep the server on
> `127.0.0.1` (the default) and do not expose it to an untrusted network:
> anyone who can reach it can approve and send drafts.

| Page | What you can do |
| --- | --- |
| **Overview** (`/ui/overview`, where `/ui/` lands) | Real totals (companies, prospects, campaigns, drafts per state), drafts awaiting review, recent delivery activity, and next steps derived from your data. Uncertain deliveries are always listed first. |
| **Explorer** (`/ui/explorer`) | Search with the configured provider. Open a company to see its discovery provenance, research its stored website (or refresh it), run qualification with your criteria, read each factor's evidence, and save it once the latest run is *qualified*. |
| **Saved prospects** (`/ui/prospects`) | Filter by text, domain, score range, and saved date; open a prospect to see its justifying qualification and research history. |
| **Campaigns** (`/ui/campaigns`) | Create a campaign from saved prospects, then press **Generate drafts**. Generation never happens on page load and never approves or sends. |
| **Draft review** (`/ui/drafts`) | Tabs for drafts awaiting approval, approved, needing attention, and sent. A draft page shows the exact subject and body, its provenance, the approval, the delivery controls, and every attempt. |
| **Delivery history** (`/ui/deliveries`) | Every dry run, real send, and manual resolution, filterable by outcome and mode. |
| **System status** (`/ui/system`) | Providers, email mode, database reachability, and migration revision. It shows only whether secrets are set, never their values. |

**Approving and sending a draft** takes separate, explicit steps:

1. Open the draft and read the subject and the full body.
2. Enter the recipient yourself, tick *I reviewed…*, and press **Approve
   this draft**. This records the approval only; nothing is sent.
3. Press **Record a dry run** to log a simulated attempt, or, only when real
   delivery is enabled (see 5f), tick the confirmation and press **Send
   email**. A sent draft is never sent again.
4. If the page says **Delivery uncertain**, the email may already have been
   accepted. Look for the shown Message-ID in your Sent folder or mail
   server logs, then record what you found with **Record verified
   outcome**. Explee never resends on its own.

The banner at the top of every page shows the email mode. The default,
*dry-run only, nothing is sent*, comes from `EMAIL_PROVIDER=fake` and
`EMAIL_DRY_RUN=true`. No new settings are needed for the UI.

All forms work without JavaScript; HTMX only makes them faster. Every form
is protected against cross-site requests. If the server restarted while a
page was open, the next submission says the form expired: reload the page
and submit again.

**Rebuilding the stylesheet** (only after changing templates or
`app/static/src/app.css`). The project uses the Tailwind CSS v4.3.3
standalone executable, which needs no Node.js:

1. Download `tailwindcss-windows-x64.exe` from
   <https://github.com/tailwindlabs/tailwindcss/releases/tag/v4.3.3> into
   `tools/tailwind/`. That folder is git-ignored. Optionally check it
   against the release's `sha256sums.txt`.
2. Run `python -m scripts.build_css`, or `python -m scripts.build_css
   --watch` while editing. You can also pass `--tailwind PATH` to use the
   executable.
3. Commit the updated `app/static/css/app.css`.

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
Mistral credentials. Tavily, the website fetcher, and the Mistral adapter
are exercised only through HTTPX mock transports, injected DNS resolvers,
fakes, and an HTML fixture in `tests/fixtures/websites/`. Test settings use
`SEARCH_PROVIDER=fake`, `WEBSITE_FETCHER=fake`, `DRAFT_GENERATOR=fake`, and
`EMAIL_PROVIDER=fake`; no test calls the real Mistral API or sends email.
The web interface tests drive the full app through HTTPX (pages, HTMX
fragments, CSRF, approval and delivery flows) against the same disposable
database. PostgreSQL-backed integration tests
(discovery, research, qualification, campaign and draft persistence, API
persistence, migrations) run only when
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
| A UI page says "The database could not be reached or is not migrated" | PostgreSQL is not running, `DATABASE_URL` is wrong, or migrations are missing. Run `alembic upgrade head`; **System status** shows the current and expected revision. |
| A UI form says it "has expired" or "security token is not valid" | The server restarted (tokens are per process) or cookies are blocked for `127.0.0.1`. Reload the page and submit again. |
| A UI page has no styling | `/static/css/app.css` is missing. Restore it from Git, or rebuild it with `python -m scripts.build_css` (see 6b). |
| Explorer search says the provider is not configured | Set `TAVILY_API_KEY` (or `SEARCH_PROVIDER=fake`, which returns no results) and restart. |
| Draft generation says the provider is not configured | Set `MISTRAL_API_KEY`, or `DRAFT_GENERATOR=fake` for offline drafts, and restart. |
| The draft page only offers a dry run | Real delivery needs `EMAIL_PROVIDER=smtp`, `EMAIL_DRY_RUN=false` and `EMAIL_SENDER`; the page lists what is missing. |
| A draft shows **Delivery uncertain** | The message may have been accepted. Check the Message-ID in your Sent folder or mail logs, then record the outcome on the draft page (see 5f). |

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
    campaign.py           campaigns and their selected prospects
    outreach_draft.py     generated drafts and their delivery state
    draft_approval.py     recorded approvals (recipient + content hash)
    email_send_attempt.py delivery audit trail
  schemas/           Pydantic request/response schemas
  routers/           thin HTTP routes (health, discovery, research,
                     qualification, prospects, campaigns, drafts)
    ui/                   web interface routes under /ui
  web/               UI infrastructure: CSRF, form parsing, rendering,
                     security headers, presentation labels
  templates/         Jinja2 templates (base layout, macros, pages,
                     HTMX fragments)
  static/            compiled Tailwind CSS, its source, vendored HTMX
  services/          business logic
    discovery.py          search -> normalize -> dedupe -> persist
    discovery_urls.py     URL/domain normalization
    research.py           fetch stored website -> persist attempt -> prune
    enrichment.py         read-only discovery + research projection
    qualification_rules.py  pure, versioned scoring rules
    qualification.py      run + persist qualification, history
    prospects.py          eligibility-gated save, search, retrieval
    campaigns.py          campaigns from saved prospects
    outreach_prompt.py    versioned, deterministic draft prompt
    drafts.py             generate -> validate -> persist pending drafts
    approvals.py          approval gate and content hash
    delivery.py           claim -> send -> finalize; manual resolution
    providers/            SearchProvider (tavily/fake), WebsiteFetcher
                          (httpx/fake), DraftGenerator (mistral/fake),
                          EmailProvider (smtp/fake), SSRF checks,
                          HTML text
alembic/             migration environment (URL from app settings)
docs/                architecture and phased plan
scripts/             operational helpers (check_db, build_css)
tests/unit/          fast, isolated tests
tests/integration/   ASGI app tests; DB tests when TEST_DATABASE_URL is set
tests/fixtures/      recorded HTML fixtures (no live websites in tests)
```

See `docs/architecture.md` for boundaries and `docs/implementation-plan.md`
for the roadmap.
