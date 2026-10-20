"""Verify Phase 2 upgrades a database created by the Phase 1 migration.

Requires ``TEST_DATABASE_URL`` pointing at a disposable database. The test
drops the local schema, applies ``0001_company_candidates``, inserts a
candidate, then applies ``head`` and checks that the existing row survived.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import PROJECT_ROOT
from app.models import Base

_DROP_SCHEMA = text(
    "DROP TABLE IF EXISTS research_records CASCADE; "
    "DROP TABLE IF EXISTS company_candidates CASCADE; "
    "DROP TABLE IF EXISTS alembic_version CASCADE;"
)


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_phase1_database_upgrades_without_losing_candidates() -> None:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping migration test")

    engine = create_engine(url)
    try:
        try:
            with engine.begin() as connection:
                connection.execute(_DROP_SCHEMA)
        except SQLAlchemyError as exc:
            message = str(exc).splitlines()[0]
            pytest.skip(f"Test database is unreachable: {message[:200]}")

        config = _alembic_config(url)
        command.upgrade(config, "0001_company_candidates")

        with engine.begin() as connection:
            candidate_id = connection.execute(
                text(
                    "INSERT INTO company_candidates "
                    "(name, website_url, domain, provider, source_url) "
                    "VALUES ('Existing Co', 'https://existing.example/', "
                    "'existing.example', 'fake', 'https://existing.example/') "
                    "RETURNING id"
                )
            ).scalar_one()

        command.upgrade(config, "head")

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT name, website_url, domain, source_url "
                    "FROM company_candidates WHERE id = :id"
                ),
                {"id": candidate_id},
            ).one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert row.name == "Existing Co"
        assert row.website_url == "https://existing.example/"
        assert row.domain == "existing.example"
        assert row.source_url == "https://existing.example/"
        assert revision == "0002_research_records"
        assert "research_records" in inspect(engine).get_table_names()
    finally:
        # Leave a schema matching the ORM for tests that run afterwards.
        try:
            with engine.begin() as connection:
                connection.execute(_DROP_SCHEMA)
            Base.metadata.create_all(engine)
        except SQLAlchemyError:
            pass
        engine.dispose()
