"""Verify that the configured PostgreSQL database is reachable.

Usage (from the project root, with the virtual environment active):

    python -m scripts.check_db

Reads ``DATABASE_URL`` from the environment / ``.env`` exactly like the
application. Exits with status 0 on success and 1 on failure. The password
is never printed.
"""

from __future__ import annotations

import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import build_engine


def main() -> int:
    settings = get_settings()
    url = make_url(str(settings.database_url))
    print(f"Checking database: {url.render_as_string(hide_password=True)}")

    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
            database = connection.execute(text("SELECT current_database()")).scalar_one()
    except SQLAlchemyError as exc:
        message = str(exc.__cause__ or exc).splitlines()[0]
        print(f"FAILED: {message}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"Connected to database '{database}'.")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
