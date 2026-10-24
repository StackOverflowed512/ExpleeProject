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
from sqlalchemy import CheckConstraint, Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import PROJECT_ROOT
from app.models import Base

HEAD_REVISION = "0006_check_constraint_names"
PHASE4_REVISION = "0005_campaigns_drafts"
PHASE3_PRELIMINARY_REVISION = "0004_qualification_preliminary"
PHASE3_REVISION = "0003_qualification_prospects"
PHASE2_REVISION = "0002_company_research"
PHASE1_REVISION = "0001_company_candidates"

_DROP_SCHEMA = text(
    "DROP TABLE IF EXISTS outreach_drafts CASCADE; "
    "DROP TABLE IF EXISTS campaign_prospects CASCADE; "
    "DROP TABLE IF EXISTS campaigns CASCADE; "
    "DROP TABLE IF EXISTS saved_prospects CASCADE; "
    "DROP TABLE IF EXISTS company_qualifications CASCADE; "
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


# -- Phase 3: qualifications and saved prospects ---------------------------
def test_qualification_and_prospect_tables_exist(inspector: Engine) -> None:
    schema = inspect(inspector)
    qualification_columns = {c["name"] for c in schema.get_columns("company_qualifications")}
    assert qualification_columns == {
        "id",
        "candidate_id",
        "research_id",
        "rule_version",
        "criteria",
        "status",
        "score",
        "threshold",
        "evidence_coverage",
        "factors",
        "rationale",
        "qualified_at",
        "created_at",
        "updated_at",
    }
    prospect_columns = {c["name"] for c in schema.get_columns("saved_prospects")}
    assert prospect_columns == {"id", "candidate_id", "qualification_id", "saved_at", "updated_at"}

    unique = {
        u["name"]: u["column_names"] for u in schema.get_unique_constraints("saved_prospects")
    }
    assert unique["uq_saved_prospects_candidate_id"] == ["candidate_id"]
    foreign_keys = {
        fk["constrained_columns"][0]: fk for fk in schema.get_foreign_keys("company_qualifications")
    }
    assert foreign_keys["candidate_id"]["options"].get("ondelete") == "CASCADE"
    assert foreign_keys["research_id"]["options"].get("ondelete") == "SET NULL"


_INSERT_QUALIFICATION = text(
    "INSERT INTO company_qualifications "
    "(candidate_id, rule_version, criteria, status, score, threshold, "
    "evidence_coverage, factors, rationale, qualified_at) VALUES "
    "(:cid, 'qualification-rules/v1', '{}', :status, :score, 60, 1.0, '[]', 'r', now()) "
    "RETURNING id"
)


@pytest.mark.parametrize(
    ("status", "score"),
    [("bogus", 50), ("qualified", 101), ("qualified", -1)],
)
def test_qualification_check_constraints(inspector: Engine, status: str, score: int) -> None:
    with inspector.connect() as connection:
        candidate_id = _insert_candidate(connection, f"q-{status}-{score}.example")
        with pytest.raises(IntegrityError):
            connection.execute(
                _INSERT_QUALIFICATION, {"cid": candidate_id, "status": status, "score": score}
            )
        connection.rollback()


def test_saved_prospect_is_unique_per_candidate_and_cascades(inspector: Engine) -> None:
    insert = text(
        "INSERT INTO saved_prospects (candidate_id, qualification_id) VALUES (:cid, :qid)"
    )
    with inspector.connect() as connection:
        candidate_id = _insert_candidate(connection, "unique-prospect.example")
        qualification_id = connection.execute(
            _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 80}
        ).scalar_one()
        connection.execute(insert, {"cid": candidate_id, "qid": qualification_id})
        connection.commit()
        with pytest.raises(IntegrityError):
            connection.execute(insert, {"cid": candidate_id, "qid": qualification_id})
        connection.rollback()

        connection.execute(
            text("DELETE FROM company_candidates WHERE id = :cid"), {"cid": candidate_id}
        )
        remaining = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM saved_prospects WHERE candidate_id = :cid) + "
                "(SELECT count(*) FROM company_qualifications WHERE candidate_id = :cid)"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        connection.commit()
    assert remaining == 0


def test_justifying_qualification_cannot_be_deleted_alone(inspector: Engine) -> None:
    with inspector.connect() as connection:
        candidate_id = _insert_candidate(connection, "restrict.example")
        qualification_id = connection.execute(
            _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 90}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO saved_prospects (candidate_id, qualification_id) VALUES (:c, :q)"),
            {"c": candidate_id, "q": qualification_id},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text("DELETE FROM company_qualifications WHERE id = :q"), {"q": qualification_id}
            )
        connection.rollback()


def test_pruned_research_nulls_qualification_reference(inspector: Engine) -> None:
    with inspector.begin() as connection:
        candidate_id = _insert_candidate(connection, "set-null.example")
        research_id = connection.execute(
            text(
                "INSERT INTO company_research "
                "(candidate_id, requested_url, fetched_at, fetcher, outcome) "
                "VALUES (:cid, 'https://set-null.example/', now(), 'fake', 'success') "
                "RETURNING id"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        qualification_id = connection.execute(
            _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 70}
        ).scalar_one()
        connection.execute(
            text("UPDATE company_qualifications SET research_id = :r WHERE id = :q"),
            {"r": research_id, "q": qualification_id},
        )
        connection.execute(text("DELETE FROM company_research WHERE id = :r"), {"r": research_id})
        reference = connection.execute(
            text("SELECT research_id FROM company_qualifications WHERE id = :q"),
            {"q": qualification_id},
        ).scalar_one()
    assert reference is None


def test_upgrade_from_phase2_schema_preserves_candidates_and_research(
    migration_database: str,
) -> None:
    """A database at the Phase 2 revision with data upgrades without loss."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, PHASE2_REVISION)
        with engine.begin() as connection:
            tables = inspect(connection).get_table_names()
            assert "company_qualifications" not in tables
            assert "saved_prospects" not in tables
            candidate_id = _insert_candidate(connection, "phase2.example")
            connection.execute(
                _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "success", "failure": None}
            )
            connection.execute(
                _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "failed", "failure": "timeout"}
            )

        command.upgrade(config, "head")
        with engine.begin() as connection:
            name = connection.execute(
                text("SELECT name FROM company_candidates WHERE id = :cid"), {"cid": candidate_id}
            ).scalar_one()
            outcomes = connection.execute(
                text("SELECT outcome FROM company_research WHERE candidate_id = :cid ORDER BY id"),
                {"cid": candidate_id},
            ).scalars().all()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert name == "Acme"
        assert outcomes == ["success", "failed"]
        assert revision == HEAD_REVISION

        command.downgrade(config, PHASE2_REVISION)
        with engine.begin() as connection:
            tables = inspect(connection).get_table_names()
            research_rows = connection.execute(
                text("SELECT count(*) FROM company_research")
            ).scalar_one()
        assert "company_qualifications" not in tables
        assert "saved_prospects" not in tables
        assert research_rows == 2
    finally:
        command.upgrade(config, "head")
        engine.dispose()


# -- 0004: preliminary qualification status --------------------------------------
def test_preliminary_status_is_accepted_at_head(inspector: Engine) -> None:
    with inspector.begin() as connection:
        candidate_id = _insert_candidate(connection, "preliminary.example")
        qualification_id = connection.execute(
            _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "preliminary", "score": 100}
        ).scalar_one()
        connection.execute(
            text("DELETE FROM company_qualifications WHERE id = :q"), {"q": qualification_id}
        )


def test_upgrade_from_phase3_schema_preserves_all_rows(migration_database: str) -> None:
    """A database at 0003 with candidates, research, runs, and a prospect upgrades intact."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    count_all = text(
        "SELECT (SELECT count(*) FROM company_candidates), "
        "(SELECT count(*) FROM company_research), "
        "(SELECT count(*) FROM company_qualifications), "
        "(SELECT count(*) FROM saved_prospects)"
    )
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, PHASE3_REVISION)
        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection, "phase3.example")
            connection.execute(
                _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "success", "failure": None}
            )
            qualification_id = connection.execute(
                _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 90}
            ).scalar_one()
            connection.execute(
                _INSERT_QUALIFICATION,
                {"cid": candidate_id, "status": "insufficient_evidence", "score": 0},
            )
            connection.execute(
                text(
                    "INSERT INTO saved_prospects (candidate_id, qualification_id) "
                    "VALUES (:c, :q)"
                ),
                {"c": candidate_id, "q": qualification_id},
            )
            before = tuple(connection.execute(count_all).one())
            # 0003 rejects the new status.
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        _INSERT_QUALIFICATION,
                        {"cid": candidate_id, "status": "preliminary", "score": 100},
                    )

        command.upgrade(config, "head")
        with engine.begin() as connection:
            after = tuple(connection.execute(count_all).one())
            statuses = connection.execute(
                text("SELECT status FROM company_qualifications ORDER BY id")
            ).scalars().all()
        assert before == after == (1, 1, 2, 1)
        assert statuses == ["qualified", "insufficient_evidence"]
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_downgrade_refuses_to_drop_preliminary_rows(migration_database: str) -> None:
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection, "no-downgrade.example")
            connection.execute(
                _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "preliminary", "score": 100}
            )
        with pytest.raises(IntegrityError):
            command.downgrade(config, PHASE3_REVISION)
        with engine.begin() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            preliminary = connection.execute(
                text("SELECT count(*) FROM company_qualifications WHERE status = 'preliminary'")
            ).scalar_one()
            connection.execute(
                text("DELETE FROM company_candidates WHERE id = :cid"), {"cid": candidate_id}
            )
        assert revision == HEAD_REVISION
        assert preliminary == 1
    finally:
        command.upgrade(config, "head")
        engine.dispose()


# -- 0005: campaigns and outreach drafts --------------------------------------------
def test_outreach_tables_and_constraints_exist(inspector: Engine) -> None:
    schema = inspect(inspector)
    tables = set(schema.get_table_names())
    assert {"campaigns", "campaign_prospects", "outreach_drafts"} <= tables

    assert schema.get_pk_constraint("campaign_prospects")["constrained_columns"] == [
        "campaign_id",
        "saved_prospect_id",
    ]
    uniques = {
        u["name"]: u["column_names"]
        for u in schema.get_unique_constraints("outreach_drafts")
    }
    assert uniques["uq_outreach_drafts_campaign_prospect_revision"] == [
        "campaign_id",
        "saved_prospect_id",
        "revision",
    ]
    foreign_keys = {fk["name"]: fk for fk in schema.get_foreign_keys("outreach_drafts")}
    composite = foreign_keys["fk_outreach_drafts_campaign_prospect"]
    assert composite["referred_table"] == "campaign_prospects"
    assert composite["constrained_columns"] == ["campaign_id", "saved_prospect_id"]
    assert composite["options"].get("ondelete") == "CASCADE"
    # Exact names: the migration must match the model's naming convention
    # (no doubled "ck_<table>_ck_<table>_" prefix).
    assert {c["name"] for c in schema.get_check_constraints("outreach_drafts")} == {
        "ck_outreach_drafts_state_valid",
        "ck_outreach_drafts_revision_positive",
        "ck_outreach_drafts_subject_not_blank",
        "ck_outreach_drafts_body_not_blank",
    }
    assert {c["name"] for c in schema.get_check_constraints("campaigns")} == {
        "ck_campaigns_tone_valid"
    }
    assert {c["name"] for c in schema.get_check_constraints("campaign_prospects")} == {
        "ck_campaign_prospects_position_non_negative"
    }


def test_upgrade_from_phase3_head_preserves_all_rows(migration_database: str) -> None:
    """A database at 0004 with Phase 1-3 data upgrades to 0005 intact."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    count_all = text(
        "SELECT (SELECT count(*) FROM company_candidates), "
        "(SELECT count(*) FROM company_research), "
        "(SELECT count(*) FROM company_qualifications), "
        "(SELECT count(*) FROM saved_prospects)"
    )
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, PHASE3_PRELIMINARY_REVISION)
        with engine.begin() as connection:
            assert "campaigns" not in inspect(connection).get_table_names()
            candidate_id = _insert_candidate(connection, "phase4.example")
            connection.execute(
                _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "success", "failure": None}
            )
            qualification_id = connection.execute(
                _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 90}
            ).scalar_one()
            connection.execute(
                _INSERT_QUALIFICATION,
                {"cid": candidate_id, "status": "preliminary", "score": 100},
            )
            connection.execute(
                text(
                    "INSERT INTO saved_prospects (candidate_id, qualification_id) "
                    "VALUES (:c, :q)"
                ),
                {"c": candidate_id, "q": qualification_id},
            )
            before = tuple(connection.execute(count_all).one())

        command.upgrade(config, "head")
        with engine.begin() as connection:
            after = tuple(connection.execute(count_all).one())
            campaigns = connection.execute(text("SELECT count(*) FROM campaigns")).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert before == after == (1, 1, 2, 1)
        assert campaigns == 0
        assert revision == HEAD_REVISION

        command.downgrade(config, PHASE3_PRELIMINARY_REVISION)
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            restored = tuple(connection.execute(count_all).one())
        assert not {"campaigns", "campaign_prospects", "outreach_drafts"} & tables
        assert restored == (1, 1, 2, 1)
    finally:
        command.upgrade(config, "head")
        engine.dispose()


# -- 0006: CHECK constraint names --------------------------------------------------
def _model_check_names() -> dict[str, set[str]]:
    """CHECK names the models produce via the ``ck`` naming convention."""
    expected: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        prefix = f"ck_{table.name}_"
        names = set()
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            # After create_all has run, SQLAlchemy has already replaced the
            # raw name ("status_valid") with the final one; never prefix twice.
            name = str(constraint.name)
            names.add(name if name.startswith(prefix) else prefix + name)
        if names:
            expected[table.name] = names
    return expected


def _database_check_names(engine: Engine) -> dict[str, set[str]]:
    schema = inspect(engine)
    found: dict[str, set[str]] = {}
    for table in schema.get_table_names():
        names = {c["name"] for c in schema.get_check_constraints(table)}
        if names:
            found[table] = names
    return found


def test_check_constraint_names_match_models_at_head(inspector: Engine) -> None:
    expected = _model_check_names()
    assert set(expected) == {
        "company_research",
        "company_qualifications",
        "campaigns",
        "campaign_prospects",
        "outreach_drafts",
    }
    assert _database_check_names(inspector) == expected


def test_constraint_rename_round_trip_restores_original_names(
    migration_database: str,
) -> None:
    """Upgrading fixes the names; downgrading restores the exact old ones."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    count_all = text(
        "SELECT (SELECT count(*) FROM company_candidates), "
        "(SELECT count(*) FROM company_research), "
        "(SELECT count(*) FROM company_qualifications)"
    )
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, PHASE4_REVISION)
        original = _database_check_names(engine)
        assert "ck_company_qualifications_ck_company_qualifications_score_range" in original[
            "company_qualifications"
        ]
        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection, "rename.example")
            connection.execute(
                _INSERT_RESEARCH, {"cid": candidate_id, "outcome": "success", "failure": None}
            )
            connection.execute(
                _INSERT_QUALIFICATION, {"cid": candidate_id, "status": "qualified", "score": 80}
            )
            before = tuple(connection.execute(count_all).one())

        command.upgrade(config, "head")
        assert _database_check_names(engine) == _model_check_names()
        with engine.begin() as connection:
            assert tuple(connection.execute(count_all).one()) == before
            # Constraints still enforce their rules after the rename.
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        _INSERT_QUALIFICATION,
                        {"cid": candidate_id, "status": "qualified", "score": 101},
                    )

        command.downgrade(config, PHASE4_REVISION)
        assert _database_check_names(engine) == original
        with engine.begin() as connection:
            assert tuple(connection.execute(count_all).one()) == before
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_full_downgrade_still_works_after_rename(migration_database: str) -> None:
    """0004's downgrade drops a constraint by its old name; 0006 must restore it."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    try:
        command.upgrade(config, "head")
        command.downgrade(config, PHASE2_REVISION)
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
        assert "company_qualifications" not in tables
        assert "company_research" in tables
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_rename_is_a_no_op_when_names_are_already_correct(migration_database: str) -> None:
    """A create_all database stamped at 0005 keeps its (correct) names."""
    config = _alembic_config(migration_database)
    engine = create_engine(migration_database)
    try:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        Base.metadata.create_all(engine)
        command.stamp(config, PHASE4_REVISION)
        assert _database_check_names(engine) == _model_check_names()

        command.upgrade(config, "head")
        assert _database_check_names(engine) == _model_check_names()
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == HEAD_REVISION
    finally:
        with engine.begin() as connection:
            connection.execute(_DROP_SCHEMA)
        command.upgrade(config, "head")
        engine.dispose()
