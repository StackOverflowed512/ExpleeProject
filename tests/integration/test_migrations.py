"""Verify the migrations upgrade an empty database and a Phase 1 database.

Requires ``TEST_DATABASE_URL`` pointing at a disposable database. The
database must already exist; the test drops the application tables, runs
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
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import PROJECT_ROOT
from app.models import Base

HEAD_REVISION = "0002_company_research"
PHASE1_REVISION = "0001_company_candidates"

_DROP_SCHEMA = text(
    "DROP TABLE IF EXISTS company_research CASCADE; "
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
    assert revision == HEAD_REVISION


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


def test_research_table_has_expected_columns(inspector: Engine) -> None:
    columns = {c["name"]: c for c in inspect(inspector).get_columns("company_research")}
    assert set(columns) == {
        "id",
        "candidate_id",
        "requested_url",
        "final_url",
        "fetched_at",
        "fetcher",
        "outcome",
        "failure_category",
        "http_status",
        "content_type",
        "page_title",
        "extracted_text",
        "content_truncated",
        "created_at",
        "updated_at",
    }
    for required in ("candidate_id", "requested_url", "fetched_at", "outcome", "fetcher"):
        assert columns[required]["nullable"] is False
    assert columns["final_url"]["nullable"] is True


def test_research_foreign_key_and_index(inspector: Engine) -> None:
    schema = inspect(inspector)
    (foreign_key,) = schema.get_foreign_keys("company_research")
    assert foreign_key["referred_table"] == "company_candidates"
    assert foreign_key["constrained_columns"] == ["candidate_id"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"
    index_names = {index["name"] for index in schema.get_indexes("company_research")}
    assert "ix_company_research_candidate_fetched" in index_names


def _insert_candidate(connection: Connection, domain: str) -> int:
    return connection.execute(
        text(
            "INSERT INTO company_candidates (name, domain, provider, source_url) "
            "VALUES ('Acme', :domain, 'fake', 'https://acme.example/') RETURNING id"
        ),
        {"domain": domain},
    ).scalar_one()


_INSERT_RESEARCH = text(
    "INSERT INTO company_research "
    "(candidate_id, requested_url, fetched_at, fetcher, outcome, failure_category) "
    "VALUES (:cid, 'https://acme.example/', now(), 'fake', :outcome, :failure)"
)


@pytest.mark.parametrize(
    ("outcome", "failure"),
    [("failed", None), ("success", "timeout"), ("unknown", None)],
)
def test_research_check_constraints_reject_inconsistent_rows(
    inspector: Engine, outcome: str, failure: str | None
) -> None:
    with inspector.connect() as connection:
        candidate_id = _insert_candidate(connection, f"check-{outcome}.example")
        with pytest.raises(IntegrityError):
            connection.execute(
                _INSERT_RESEARCH,
                {"cid": candidate_id, "outcome": outcome, "failure": failure},
            )
        connection.rollback()


def test_deleting_candidate_cascades_to_research(inspector: Engine) -> None:
    with inspector.begin() as connection:
        candidate_id = _insert_candidate(connection, "cascade.example")
        connection.execute(
            _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "success", "failure": None}
        )
        connection.execute(
            text("DELETE FROM company_candidates WHERE id = :cid"), {"cid": candidate_id}
        )
        remaining = connection.execute(
            text("SELECT count(*) FROM company_research WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
    assert remaining == 0


def test_upgrade_from_phase1_schema_preserves_candidates(migration_database: str) -> None:
    """A database at the Phase 1 revision with data upgrades cleanly."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, PHASE1_REVISION)
        with engine.begin() as connection:
            assert "company_research" not in inspect(connection).get_table_names()
            candidate_id = _insert_candidate(connection, "phase1.example")

        command.upgrade(config, "head")
        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT name, source_url FROM company_candidates WHERE id = :cid"),
                {"cid": candidate_id},
            ).one()
            assert tuple(row) == ("Acme", "https://acme.example/")
            assert connection.execute(
                text("SELECT count(*) FROM company_research")
            ).scalar_one() == 0

        command.downgrade(config, PHASE1_REVISION)
        with engine.begin() as connection:
            assert "company_research" not in inspect(connection).get_table_names()
            assert connection.execute(
                text("SELECT count(*) FROM company_candidates")
            ).scalar_one() == 1
    finally:
        command.upgrade(config, "head")
        engine.dispose()
