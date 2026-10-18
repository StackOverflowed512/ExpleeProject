# Explee

Personal AI-powered sales intelligence and outreach platform. Two modules:

- **Explorer** — discover companies, research their websites, enrich and
  qualify prospects, and save them.
- **Outreach Agent** — build campaigns from saved prospects, generate drafts,
  obtain explicit human approval, and send through a configurable provider.

This repository currently contains **Phase 0: foundation** — application
skeleton, configuration, health endpoint, error contract, database wiring,
migrations setup, and tests. No product features are implemented yet.

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
only database-backed features (arriving in later phases) need it.

## 5. Run database migrations

```powershell
alembic upgrade head
alembic current
```

Alembic reads `DATABASE_URL` from the same settings as the application. No
revisions exist yet in Phase 0, so `upgrade head` is a no-op that still
verifies the connection.

Useful commands:

```powershell
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic downgrade -1
alembic check
```

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

Tests are deterministic, use no network, no live database, and no Mistral
credentials.

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
  models/            SQLAlchemy declarative base (models arrive with features)
  schemas/           Pydantic request/response schemas
  routers/           thin HTTP routes
  services/          business logic; provider adapters later
alembic/             migration environment (URL from app settings)
docs/                architecture and phased plan
scripts/             operational helpers (check_db)
tests/unit/          fast, isolated tests
tests/integration/   full ASGI app tests (in-process HTTPX)
```

See `docs/architecture.md` for boundaries and `docs/implementation-plan.md`
for the roadmap.
