# CLAUDE.md

Instructions for AI-assisted sessions working in this repository.

## Before changing anything

- Inspect the repository first: current files, `git status`, branch, and the
  relevant modules. Read `docs/architecture.md` and
  `docs/implementation-plan.md`.
- Never overwrite existing files or discard uncommitted changes without
  explaining the risk and getting permission.
- Do not commit, push, or create branches unless the user asks.

## Architecture and conventions

- FastAPI app is built by `create_app(settings)` in `app/main.py`; the
  module-level `app` exists only for `uvicorn app.main:app`.
- Routes live in `app/routers/`, stay thin, and delegate to services.
  Business logic belongs in `app/services/`.
- Request/response bodies are Pydantic schemas in `app/schemas/`.
- ORM models use the declarative `Base` in `app/models/base.py`; import new
  models into `app/models/__init__.py` so Alembic sees them.
- Database access goes through the `get_session` dependency; one session per
  request, closed at the end.
- Configuration lives only in `app/config.py` (Pydantic Settings + `.env`).
  Add new settings there with types, defaults, and validation.
- Errors use the envelope in `app/errors.py`; raise `AppError` with a
  stable snake_case `code`.
- External capabilities go behind protocols. Discovery selects a
  `SearchProvider` through `SEARCH_PROVIDER`: `tavily` is the real adapter
  and `fake` is the credential-free/test adapter. Business logic never reads
  vendor payloads.
- Match the existing naming, typing, and docstring style. Python 3.12+;
  full type hints on public functions.

## Engineering rules

- Validate all external input and use type hints everywhere.
- Write tests for every new behavior:
  - `tests/unit/` — no network, no database.
  - `tests/integration/` — full ASGI app through HTTPX.
  - Tests must be deterministic and must not require real Tavily, Mistral,
    or email credentials. Use HTTPX mock transports or fakes for providers.
- Never hardcode secrets. Read them from settings; never log credentials,
  tokens, or full connection URLs.
- Never invent external-provider data. Persist provenance with every fact
  and leave unknown fields empty.
- Never send email without explicit human approval enforced by the backend
  service layer — not only by the UI. Approval is a recorded state
  transition.
- Never introduce Docker, containers, or container orchestration.
- Do not add unrelated refactors, speculative abstractions, or dependencies.
  Prefer the standard library and already-declared packages.
- Do not implement future phases early. Keep the phase boundaries in
  `docs/implementation-plan.md`.
- Do not install system software or change machine-wide settings without
  asking. A local virtual environment is fine.

## Verification

- Run `pytest` before reporting completion and report actual results; never
  claim unexecuted tests passed.
- If PostgreSQL is unavailable, say exactly what could not be verified.
- Update `docs/implementation-plan.md` and `README.md` after each completed
  phase, including any new setup steps.

## Stack constraints

Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy 2.x, Alembic, Pydantic 2.x,
Pydantic Settings, pytest, HTTPX, and (in later phases) Jinja2 + HTMX.
Mistral is the AI provider. Local virtual environment; no Docker. No React,
Node.js, Redis, Celery, or agent frameworks unless a future phase explicitly
changes this.
