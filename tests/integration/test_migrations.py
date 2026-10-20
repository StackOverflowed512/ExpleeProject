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
    "DROP TABLE IF EXISTS research_records CASCADE; "
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
    assert revision == "0002_research_records"


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


def test_migration_creates_research_records(inspector: Engine) -> None:
    tables = inspect(inspector).get_table_names()
    assert "research_records" in tables


def test_research_columns_exist(inspector: Engine) -> None:
    columns = {
        column["name"]: column
        for column in inspect(inspector).get_columns("research_records")
    }
    assert set(columns) == {
        "id",
        "company_candidate_id",
        "requested_url",
        "final_url",
        "fetched_at",
        "http_status",
        "content_type",
        "page_title",
        "page_text",
        "outcome",
        "failure_category",
        "truncated",
        "content_hash",
        "attempt_key",
        "created_at",
        "updated_at",
    }
    for required in (
        "company_candidate_id",
        "requested_url",
        "fetched_at",
        "outcome",
        "truncated",
        "attempt_key",
        "created_at",
        "updated_at",
    ):
        assert columns[required]["nullable"] is False
    for optional in (
        "final_url",
        "http_status",
        "content_type",
        "page_title",
        "page_text",
        "failure_category",
        "content_hash",
    ):
        assert columns[optional]["nullable"] is True


def test_research_foreign_key_cascades(inspector: Engine) -> None:
    foreign_keys = inspect(inspector).get_foreign_keys("research_records")
    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert foreign_key["constrained_columns"] == ["company_candidate_id"]
    assert foreign_key["referred_table"] == "company_candidates"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"


def test_research_attempt_uniqueness_is_enforced(inspector: Engine) -> None:
    unique_constraints = {
        constraint["name"]: constraint
        for constraint in inspect(inspector).get_unique_constraints(
            "research_records"
        )
    }
    assert "uq_research_records_candidate_attempt" in unique_constraints
    assert set(
        unique_constraints["uq_research_records_candidate_attempt"][
            "column_names"
        ]
    ) == {"company_candidate_id", "attempt_key"}

    with inspector.begin() as connection:
        candidate_id = connection.execute(
            text(
                "INSERT INTO company_candidates "
                "(name, provider, source_url) VALUES ('Acme', 'fake', 'https://acme.com') "
                "RETURNING id"
            )
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO research_records "
                "(company_candidate_id, requested_url, fetched_at, outcome, attempt_key) "
                "VALUES (:candidate_id, 'https://acme.com/', now(), 'success', 'key-1')"
            ),
            {"candidate_id": candidate_id},
        )
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO research_records "
                    "(company_candidate_id, requested_url, fetched_at, outcome, attempt_key) "
                    "VALUES (:candidate_id, 'https://acme.com/', now(), 'success', 'key-1')"
                ),
                {"candidate_id": candidate_id},
            )


def test_research_candidate_index_exists(inspector: Engine) -> None:
    indexes = {
        index["name"]: index
        for index in inspect(inspector).get_indexes("research_records")
    }
    assert "ix_research_records_candidate_fetched" in indexes
    assert indexes["ix_research_records_candidate_fetched"]["column_names"] == [
        "company_candidate_id",
        "fetched_at",
    ]


def test_alembic_check_reports_no_pending_changes(
    migration_database: str,
) -> None:
    # Raises AutogenerateDiffsDetected if the ORM metadata and schema differ.
    command.check(_alembic_config(migration_database))
