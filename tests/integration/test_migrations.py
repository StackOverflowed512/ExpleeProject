"""Verify the first migration upgrades an empty database.

Requires ``TEST_DATABASE_URL`` pointing at a disposable database. The
database must already exist; the test drops the Phase 1 tables, runs
``alembic upgrade head``, and inspects the resulting schema. It skips when
the database is missing or unreachable so a developer without PostgreSQL
can still run the rest of the suite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import PROJECT_ROOT
from app.models import Base

_DROP_SCHEMA = text(
    "DROP TABLE IF EXISTS company_candidates CASCADE; "
    "DROP TABLE IF EXISTS alembic_version CASCADE;"
)


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def migration_database() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping migration test")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(_DROP_SCHEMA)
            connection.commit()
    except SQLAlchemyError as exc:
        message = str(exc).splitlines()[0]
        pytest.skip(f"Test database is unreachable: {message[:200]}")
    finally:
        engine.dispose()

    command.upgrade(_alembic_config(url), "head")
    try:
        yield url
    finally:
        # Restore a clean schema for tests that run after this module.
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                connection.execute(_DROP_SCHEMA)
                connection.commit()
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()


@pytest.fixture
def inspector(migration_database: str) -> Iterator[Engine]:
    engine = create_engine(migration_database)
    try:
        yield engine
    finally:
        engine.dispose()


def test_migration_creates_company_candidates(inspector: Engine) -> None:
    tables = inspect(inspector).get_table_names()
    assert "company_candidates" in tables


def test_migration_is_recorded_at_head(inspector: Engine) -> None:
    with inspector.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == "0001_company_candidates"


def test_expected_columns_exist(inspector: Engine) -> None:
    columns = {c["name"]: c for c in inspect(inspector).get_columns("company_candidates")}
    assert set(columns) == {
        "id",
        "name",
        "website_url",
        "domain",
        "description",
        "provider",
        "provider_result_id",
        "source_url",
        "discovered_at",
        "updated_at",
    }
    assert columns["name"]["nullable"] is False
    assert columns["provider"]["nullable"] is False
    assert columns["source_url"]["nullable"] is False
    assert columns["domain"]["nullable"] is True


def test_unique_partial_indexes_exist(inspector: Engine) -> None:
    indexes = {
        index["name"]: index
        for index in inspect(inspector).get_indexes("company_candidates")
    }
    assert indexes["uq_company_candidates_domain"]["unique"] is True
    assert indexes["uq_company_candidates_provider_result"]["unique"] is True


def test_domain_uniqueness_is_enforced(inspector: Engine) -> None:
    with inspector.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company_candidates "
                "(name, domain, provider, source_url) "
                "VALUES ('One', 'a.com', 'fake', 'https://a.com')"
            )
        )
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO company_candidates "
                    "(name, domain, provider, source_url) "
                    "VALUES ('Two', 'a.com', 'fake', 'https://a.com')"
                )
            )


def test_null_domains_do_not_conflict(inspector: Engine) -> None:
    with inspector.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company_candidates "
                "(name, provider, source_url) VALUES ('A', 'fake', 'https://a.com')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO company_candidates "
                "(name, provider, source_url) VALUES ('B', 'fake', 'https://b.com')"
            )
        )
        count = connection.execute(
            text("SELECT count(*) FROM company_candidates WHERE domain IS NULL")
        ).scalar_one()
    assert count == 2
