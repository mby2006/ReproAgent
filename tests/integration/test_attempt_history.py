"""Integration: apply and verify the real append-only attempt-history migration.

The fixture creates disposable databases on the configured test Postgres server,
applies the repository's actual SQL migrations, seeds pre-Task-7 rows, and then
applies ``20261126090000_attempt_history.sql``. No backfill/constraint SQL is
copied into this test. The databases are dropped after the session.

Set REPRO_TEST_DATABASE_URL to a throwaway Supabase/Postgres server (the default
is the local Supabase port). The connecting role must be able to create/drop
databases. Run: uv run pytest tests/integration/test_attempt_history.py -m integration -v
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration

DSN = os.environ.get(
    "REPRO_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:54322/postgres",
)
ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((ROOT / "infra/supabase/migrations").glob("*.sql"))
ATTEMPT_MIGRATION = next(
    path for path in MIGRATIONS if path.name == "20261126090000_attempt_history.sql"
)
ATTEMPT_INDEX = MIGRATIONS.index(ATTEMPT_MIGRATION)


def _apply_migrations(connection, migrations):
    for migration in migrations:
        connection.execute(migration.read_text(encoding="utf-8"))


def _seed_legacy(db):
    """Seed rows using only the pre-Task-7 schema, including artifact ordinals."""
    user_id, project_id, paper_id, spec_id, experiment_id, plan_id = (
        uuid4() for _ in range(6)
    )
    run_ids = [uuid4(), uuid4()]
    db.execute(
        "insert into auth.users (id, email) values (%s, %s)",
        (str(user_id), f"attempt-{user_id}@example.com"),
    )
    db.execute(
        "insert into public.projects (id, user_id, name) values (%s, %s, %s)",
        (str(project_id), str(user_id), "attempt-history-project"),
    )
    db.execute(
        "insert into public.papers "
        "(id, project_id, title, pdf_path) values (%s, %s, %s, %s)",
        (str(paper_id), str(project_id), "Attempt History Paper", "paper.pdf"),
    )
    db.execute(
        "insert into public.reproduction_specs "
        "(id, paper_id, version, spec_json, origin, status) "
        "values (%s, %s, 1, '{}'::jsonb, 'extraction', 'confirmed')",
        (str(spec_id), str(paper_id)),
    )
    db.execute(
        "insert into public.experiments "
        "(id, project_id, paper_id, spec_id, name, status) "
        "values (%s, %s, %s, %s, 'attempt-hist-exp', 'planning')",
        (str(experiment_id), str(project_id), str(paper_id), str(spec_id)),
    )
    db.execute(
        "insert into public.experiment_plans "
        "(id, experiment_id, version, config_json, approved_at) "
        "values (%s, %s, 1, '{}'::jsonb, %s)",
        (str(plan_id), str(experiment_id), datetime.now(UTC)),
    )
    file_ids = []
    for index, run_id in enumerate(run_ids):
        db.execute(
            "insert into public.execution_runs "
            "(id, experiment_id, plan_id, code_version, plan_version, status, "
            " repair_attempt, runner_run_id) "
            "values (%s, %s, %s, 1, 1, 'failed', 1, %s)",
            (str(run_id), str(experiment_id), str(plan_id), uuid4()),
        )
        fid = uuid4()
        file_ids.append(str(fid))
        db.execute(
            "insert into public.generated_files "
            "(id, experiment_id, version, path, content, sha256, change_reason) "
            "values (%s, %s, 1, %s, 'x', %s, 'seed')",
            (str(fid), str(experiment_id), f"legacy/{index}.py",
             hashlib.sha256(str(fid).encode()).hexdigest()),
        )
        db.execute(
            "insert into public.execution_run_files (run_id, generated_file_id) "
            "values (%s, %s)",
            (str(run_id), str(fid)),
        )
        # NULL and metadata attempt=0 map to history attempt=1; metadata attempt=1
        # maps to history attempt=2. The latter has no parent before migration.
        for metadata, path_attempt in ((None, 0), (0, 0), (1, 1)):
            db.execute(
                "insert into public.execution_artifacts "
                "(run_id, artifact_type, storage_path, metadata_json) "
                "values (%s, 'metrics.json', %s, %s::jsonb)",
                (str(run_id), f"{run_id}/attempt-{path_attempt}/artifacts/metrics.json",
                 json.dumps({"attempt": metadata})),
            )
    return {
        "user_id": str(user_id),
        "project_id": str(project_id),
        "experiment_id": str(experiment_id),
        "run_ids": [str(run_id) for run_id in run_ids],
        "file_ids": file_ids,
    }


@pytest.fixture(scope="session")
def migrated_database():
    """Apply all real migrations around a seeded legacy database, then drop it."""
    name = f"reproagent_attempt_history_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row, connect_timeout=5) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(
                DSN, dbname=name, autocommit=True, row_factory=dict_row, connect_timeout=5
            ) as connection:
                _apply_migrations(connection, MIGRATIONS[:ATTEMPT_INDEX])
                context = _seed_legacy(connection)
                # This is the production migration under test, not equivalent SQL.
                _apply_migrations(connection, [ATTEMPT_MIGRATION])
                _apply_migrations(connection, MIGRATIONS[ATTEMPT_INDEX + 1:])
                connection.autocommit = False
                yield connection, context
        finally:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


@pytest.fixture
def db(migrated_database):
    connection, _ = migrated_database
    with connection.cursor() as cursor:
        yield cursor
        connection.rollback()


@pytest.fixture
def run_context(migrated_database):
    return migrated_database[1]


def _insert_files(db, experiment_id, prefix, count=2, version=1):
    ids = []
    for index in range(count):
        fid = uuid4()
        db.execute(
            "insert into public.generated_files "
            "(id, experiment_id, version, path, content, sha256, change_reason) "
            "values (%s, %s, %s, 'x', %s, 'seed')",
            (str(fid), experiment_id, version, f"{prefix}/file_{index}.py",
             hashlib.sha256(f"{prefix}-{index}".encode()).hexdigest()),
        )
        ids.append(str(fid))
    return ids


def _attempt(db, run_id, attempt=1, status="running"):
    db.execute(
        "insert into public.execution_attempts "
        "(run_id, attempt, plan_version, code_version, status) "
        "values (%s, %s, 1, %s, %s) on conflict (run_id, attempt) "
        "do update set status = excluded.status",
        (run_id, attempt, attempt, status),
    )


def _reject(db, error, statement, params=()):
    """Assert a statement fails and roll back only its savepoint."""
    db.execute("savepoint rejected_statement")
    try:
        with pytest.raises(error):
            db.execute(statement, params)
    finally:
        db.execute("rollback to savepoint rejected_statement")
        db.execute("release savepoint rejected_statement")


class TestSchemaContract:
    def test_execution_attempts_table_exists(self, db):
        db.execute(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name='execution_attempts'"
        )
        columns = {row["column_name"] for row in db.fetchall()}
        assert {
            "id", "run_id", "attempt", "plan_id", "plan_version", "code_version",
            "status", "runner_request_id", "runner_run_id", "dispatch_started_at",
            "runner_accepted_at", "finished_at", "timing_json", "error_code",
            "error_message", "created_at",
        }.issubset(columns)

    def test_execution_attempt_files_table_exists(self, db):
        db.execute(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name='execution_attempt_files'"
        )
        assert {row["column_name"] for row in db.fetchall()} == {
            "run_id", "attempt", "generated_file_id",
        }

    def test_execution_artifacts_attempt_column_added(self, db):
        db.execute(
            "select data_type, is_nullable from information_schema.columns "
            "where table_schema='public' and table_name='execution_artifacts' "
            "and column_name='attempt'"
        )
        row = db.fetchone()
        assert row is not None
        assert row["data_type"] == "integer"
        assert row["is_nullable"] == "YES"  # legacy rows may not be tagged yet

    def test_unique_run_attempt_enforced(self, db, run_context):
        _attempt(db, run_context["run_ids"][0], attempt=3)
        _reject(
            db, psycopg.errors.UniqueViolation,
            "insert into public.execution_attempts "
            "(run_id, attempt, plan_version, code_version, status) "
            "values (%s, 3, 1, 1, 'running')",
            (run_context["run_ids"][0],),
        )

    def test_attempt_must_be_positive(self, db, run_context):
        _reject(
            db, psycopg.errors.CheckViolation,
            "insert into public.execution_attempts "
            "(run_id, attempt, plan_version, code_version, status) "
            "values (%s, 0, 1, 1, 'queued')",
            (run_context["run_ids"][0],),
        )

    def test_attempt_files_require_existing_attempt(self, db, run_context):
        file_id = _insert_files(db, run_context["experiment_id"], "orphan")[0]
        _reject(
            db, psycopg.errors.ForeignKeyViolation,
            "insert into public.execution_attempt_files (run_id, attempt, generated_file_id) "
            "values (%s, 5, %s)",
            (run_context["run_ids"][0], file_id),
        )


class TestLegacyBackfill:
    def test_every_distinct_artifact_attempt_has_a_parent(self, db, run_context):
        db.execute(
            "select a.run_id, a.attempt, count(*) as artifacts "
            "from public.execution_artifacts a "
            "left join public.execution_attempts ea "
            "  on ea.run_id = a.run_id and ea.attempt = a.attempt "
            "where a.attempt is not null and ea.id is null "
            "group by a.run_id, a.attempt"
        )
        assert db.fetchall() == []
        db.execute(
            "select run_id, attempt from public.execution_attempts order by run_id, attempt"
        )
        assert {(row["run_id"], row["attempt"]) for row in db.fetchall()} == {
            (run_id, attempt)
            for run_id in run_context["run_ids"] for attempt in (1, 2)
        }

    def test_attempt_zero_paths_map_to_one_without_renaming_storage(self, db, run_context):
        run_id = run_context["run_ids"][0]
        db.execute(
            "select attempt, storage_path from public.execution_artifacts "
            "where run_id = %s order by storage_path",
            (run_id,),
        )
        rows = db.fetchall()
        assert [(row["attempt"], row["storage_path"]) for row in rows] == [
            (2, f"{run_id}/attempt-1/artifacts/metrics.json"),
            (1, f"{run_id}/attempt-0/artifacts/metrics.json"),
            (1, f"{run_id}/attempt-0/artifacts/metrics.json"),
        ]

    def test_artifact_insert_rejects_missing_parent(self, db, run_context):
        _reject(
            db, psycopg.errors.ForeignKeyViolation,
            "insert into public.execution_artifacts "
            "(run_id, attempt, artifact_type, storage_path) "
            "values (%s, 9, 'metrics.json', 'orphan')",
            (run_context["run_ids"][0],),
        )

    def test_artifact_update_rejects_missing_parent(self, db, run_context):
        _reject(
            db, psycopg.errors.ForeignKeyViolation,
            "update public.execution_artifacts set attempt = 9 where run_id = %s",
            (run_context["run_ids"][0],),
        )


class TestAppendOnlySemantics:
    def test_second_attempt_does_not_erase_first(self, db, run_context):
        run_id = run_context["run_ids"][0]
        first_ids = _insert_files(db, run_context["experiment_id"], "v1", version=3)
        second_ids = _insert_files(db, run_context["experiment_id"], "v2", version=4)
        for attempt, ids in ((3, first_ids), (4, second_ids)):
            _attempt(db, run_id, attempt)
            db.executemany(
                "insert into public.execution_attempt_files "
                "(run_id, attempt, generated_file_id) values (%s, %s, %s)",
                [(run_id, attempt, fid) for fid in ids],
            )
        db.execute(
            "select attempt, generated_file_id from public.execution_attempt_files "
            "where run_id = %s and attempt in (3, 4) order by attempt",
            (run_id,),
        )
        rows = db.fetchall()
        assert {row["generated_file_id"] for row in rows if row["attempt"] == 3} == set(first_ids)
        assert {row["generated_file_id"] for row in rows if row["attempt"] == 4} == set(second_ids)

    def test_artifact_query_is_scoped_to_attempt(self, db, run_context):
        run_id = run_context["run_ids"][0]
        db.execute(
            "select storage_path from public.execution_artifacts "
            "where run_id = %s and attempt = 2",
            (run_id,),
        )
        assert [row["storage_path"] for row in db.fetchall()] == [
            f"{run_id}/attempt-1/artifacts/metrics.json",
        ]

    @pytest.mark.parametrize("table", ["execution_attempts", "execution_attempt_files"])
    def test_direct_delete_is_rejected(self, db, run_context, table):
        _reject(
            db, psycopg.errors.RaiseException,
            sql.SQL("delete from public.{} where run_id = %s and attempt = 1").format(
                sql.Identifier(table)
            ).as_string(db.connection),
            (run_context["run_ids"][0],),
        )

    def test_run_delete_cascade_still_removes_history(self, db, run_context):
        run_id = run_context["run_ids"][0]
        db.execute("delete from public.execution_runs where id = %s", (run_id,))
        db.execute(
            "select count(*) as count from public.execution_attempts where run_id = %s",
            (run_id,),
        )
        assert db.fetchone()["count"] == 0
        db.execute(
            "select count(*) as count from public.execution_attempt_files where run_id = %s",
            (run_id,),
        )
        assert db.fetchone()["count"] == 0

    def test_project_delete_cascade_still_removes_history(self, db, run_context):
        db.execute("delete from public.projects where id = %s", (run_context["project_id"],))
        db.execute("select count(*) as count from public.execution_attempts")
        assert db.fetchone()["count"] == 0
        db.execute("select count(*) as count from public.execution_attempt_files")
        assert db.fetchone()["count"] == 0

    @pytest.mark.parametrize("patch", [
        {"attempt": 8}, {"plan_id": None}, {"plan_version": 9}, {"code_version": 9},
        {"created_at": "2000-01-01T00:00:00Z"},
    ])
    def test_attempt_identity_and_plan_metadata_are_immutable(self, db, run_context, patch):
        assignments = ", ".join(f"{column} = %s" for column in patch)
        _reject(
            db, psycopg.errors.RaiseException,
            f"update public.execution_attempts set {assignments} "
            "where run_id = %s and attempt = 1",
            (*patch.values(), run_context["run_ids"][0]),
        )

    def test_attempt_cannot_move_to_another_run(self, db, run_context):
        _reject(
            db, psycopg.errors.RaiseException,
            "update public.execution_attempts set run_id = %s "
            "where run_id = %s and attempt = 1",
            (run_context["run_ids"][1], run_context["run_ids"][0]),
        )

    @pytest.mark.parametrize("patch", [
        {"run_id": None}, {"attempt": 8}, {"generated_file_id": None},
    ])
    def test_file_binding_is_immutable(self, db, run_context, patch):
        column, value = next(iter(patch.items()))
        _reject(
            db, psycopg.errors.RaiseException,
            f"update public.execution_attempt_files set {column} = %s "
            "where run_id = %s and attempt = 1",
            (value, run_context["run_ids"][0]),
        )

    @pytest.mark.parametrize("status", ["succeeded", "failed", "timeout", "cancelled"])
    def test_sealed_terminal_attempt_cannot_be_rewritten(self, db, run_context, status):
        run_id = run_context["run_ids"][0]
        _attempt(db, run_id, attempt=3, status=status)
        _reject(
            db, psycopg.errors.RaiseException,
            "update public.execution_attempts set status = 'running', error_message = 'rewrite' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )

    def test_terminal_noop_is_allowed(self, db, run_context):
        run_id = run_context["run_ids"][0]
        _attempt(db, run_id, attempt=3, status="succeeded")
        db.execute(
            "update public.execution_attempts set status = 'succeeded' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )

    def test_open_attempt_allows_lifecycle_progression(self, db, run_context):
        run_id = run_context["run_ids"][0]
        _attempt(db, run_id, attempt=3, status="queued")
        db.execute(
            "update public.execution_attempts set status = 'dispatching', "
            "dispatch_started_at = now(), runner_request_id = gen_random_uuid() "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )
        db.execute(
            "update public.execution_attempts set status = 'running', "
            "runner_accepted_at = now(), runner_run_id = 'runner-3', timing_json = '{\"poll\": 1}' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )
        db.execute(
            "update public.execution_attempts set status = 'failed', finished_at = now(), "
            "error_code = 'EXECUTION_FAILED', error_message = 'sandbox died' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )
        db.execute(
            "select status, error_code, error_message, runner_run_id "
            "from public.execution_attempts where run_id = %s and attempt = 3",
            (run_id,),
        )
        assert db.fetchone() == {
            "status": "failed", "error_code": "EXECUTION_FAILED",
            "error_message": "sandbox died", "runner_run_id": "runner-3",
        }

    def test_open_attempt_cannot_regress_status_or_rewrite_runner_identity(
        self, db, run_context
    ):
        run_id = run_context["run_ids"][0]
        _attempt(db, run_id, attempt=3, status="running")
        db.execute(
            "update public.execution_attempts set runner_run_id = 'runner-original' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )
        _reject(
            db, psycopg.errors.RaiseException,
            "update public.execution_attempts set status = 'queued' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )
        _reject(
            db, psycopg.errors.RaiseException,
            "update public.execution_attempts set runner_run_id = 'runner-rewritten' "
            "where run_id = %s and attempt = 3",
            (run_id,),
        )


class TestProjectionRpc:
    def test_rpc_replaces_projection_without_changing_history(self, db, run_context):
        run_id = run_context["run_ids"][0]
        files = _insert_files(db, run_context["experiment_id"], "projection", count=2)
        db.execute(
            "select * from public.execution_attempts where run_id = %s order by attempt",
            (run_id,),
        )
        attempts_before = db.fetchall()
        db.execute(
            "select * from public.execution_attempt_files where run_id = %s order by attempt",
            (run_id,),
        )
        bindings_before = db.fetchall()

        db.execute(
            "select public.replace_run_projection(%s, 2, %s::uuid[])",
            (run_id, files),
        )

        db.execute(
            "select generated_file_id from public.execution_run_files "
            "where run_id = %s order by generated_file_id",
            (run_id,),
        )
        assert [row["generated_file_id"] for row in db.fetchall()] == sorted(files)
        db.execute(
            "select * from public.execution_attempts where run_id = %s order by attempt",
            (run_id,),
        )
        assert db.fetchall() == attempts_before
        db.execute(
            "select * from public.execution_attempt_files where run_id = %s order by attempt",
            (run_id,),
        )
        assert db.fetchall() == bindings_before

    def test_rpc_rejects_attempt_from_another_run(self, db, run_context):
        _attempt(db, run_context["run_ids"][0], attempt=3)
        _reject(
            db, psycopg.errors.RaiseException,
            "select public.replace_run_projection(%s, 3, array[]::uuid[])",
            (run_context["run_ids"][1],),
        )

    def test_rpc_rolls_back_delete_when_insert_fails(self, db, run_context):
        run_id = run_context["run_ids"][0]
        db.execute(
            "select generated_file_id from public.execution_run_files where run_id = %s",
            (run_id,),
        )
        before = db.fetchall()
        _reject(
            db, psycopg.errors.ForeignKeyViolation,
            "select public.replace_run_projection(%s, 2, array[%s::uuid])",
            (run_id, uuid4()),
        )
        db.execute(
            "select generated_file_id from public.execution_run_files where run_id = %s",
            (run_id,),
        )
        assert db.fetchall() == before

    def test_rpc_execute_is_limited_to_service_role(self, db):
        db.execute(
            "select has_function_privilege('authenticated', "
            "'public.replace_run_projection(uuid,integer,uuid[])', 'execute') as authenticated, "
            "has_function_privilege('service_role', "
            "'public.replace_run_projection(uuid,integer,uuid[])', 'execute') as service_role"
        )
        assert db.fetchone() == {"authenticated": False, "service_role": True}
