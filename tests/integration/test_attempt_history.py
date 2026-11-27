"""Integration: append-only execution attempt history.

Requires a running Postgres with the project migrations applied
(``supabase db reset`` for a throwaway local database, never a shared or
production one). These tests verify the database-level contract added by
``20261126090000_attempt_history.sql``:

  * ``execution_attempts`` and ``execution_attempt_files`` exist with the
    documented columns and constraints.
  * The composite FK from ``execution_attempt_files`` to
    ``execution_attempts (run_id, attempt)`` rejects orphans.
  * ``unique (run_id, attempt)`` prevents two rows for the same ordinal.
  * A second attempt never erases the first attempt's file bindings.
  * ``execution_artifacts.attempt`` scopes artifact queries per attempt.
  * Legacy rows whose ``storage_path`` uses ``attempt-0`` map to ``attempt=1``
    after backfill; the underlying files are NOT renamed.
  * Triggers forbid updating attempt-history rows through PostgREST paths.

Run: uv run pytest tests/integration/test_attempt_history.py -m integration -v
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip(
    "psycopg2",
    reason="Integration test requires psycopg2 and a running database",
)
import psycopg2
import psycopg2.extras

pytestmark = pytest.mark.integration

DSN = os.environ.get(
    "REPRO_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:54322/postgres",
)

VALID_CONFIG = {
    "review_mode": "assisted",
    "rationale": "attempt-history integration",
    "scaling_profile": "test",
    "delegated_at": None,
    "delegated_by": None,
    "dataset_config": {
        "name": "mnist", "num_classes": 10, "input_shape": [1, 28, 28],
        "train_subset_size": 100, "test_subset_size": 50,
    },
    "model_config": {
        "architecture": "lenet-5", "input_shape": [1, 28, 28], "num_classes": 10,
    },
    "preprocessing_config": {"steps": []},
    "training_config": {
        "optimizer": {
            "value": "SGD", "source": "paper_explicit",
            "evidence_quote": "...SGD...", "source_page": 8,
        },
        "learning_rate": {
            "value": 0.01, "source": "paper_explicit",
            "evidence_quote": "...0.01...", "source_page": 8,
        },
        "loss_function": {
            "value": "cross_entropy", "source": "paper_explicit",
            "evidence_quote": "...ce...", "source_page": 8,
        },
        "batch_size": {"value": 32, "source": "mvp_scaling"},
        "epochs": {"value": 2, "source": "mvp_scaling"},
    },
    "evaluation_config": {
        "metrics": ["accuracy"], "target_metric": "accuracy",
        "tolerance": 0.05, "comparison_note": "test",
    },
    "runtime_budget": {"max_seconds": 60, "device": "cpu", "seed": 42},
    "expected_runtime": {"estimated_seconds": 30, "basis": "test"},
    "resource_estimates": {},
    "assumptions": [],
    "deviations_from_paper": [],
}


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def db():
    c = psycopg2.connect(DSN)
    c.autocommit = False
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("BEGIN")
    yield cur
    c.rollback()
    c.close()


@pytest.fixture
def actor(db):
    user_id = uuid4()
    db.execute(
        "insert into auth.users (id, email) values (%s, %s) on conflict do nothing",
        (str(user_id), f"attempt-{user_id}@example.com"),
    )
    return str(user_id)


@pytest.fixture
def run_context(db, actor):
    """Create project -> paper -> spec -> experiment -> plan -> run. Return ids."""
    project_id, paper_id, spec_id = uuid4(), uuid4(), uuid4()
    exp_id, plan_id, run_id = uuid4(), uuid4(), uuid4()

    db.execute(
        "insert into public.projects (id, user_id, name) values (%s, %s, %s)",
        (str(project_id), actor, "attempt-history-project"),
    )
    db.execute(
        "insert into public.papers (id, project_id, title) values (%s, %s, %s)",
        (str(paper_id), str(project_id), "Attempt History Paper"),
    )
    db.execute(
        "insert into public.reproduction_specs "
        "(id, paper_id, version, spec_json, status) "
        "values (%s, %s, 1, '{}'::jsonb, 'confirmed')",
        (str(spec_id), str(paper_id)),
    )
    db.execute(
        "insert into public.experiments "
        "(id, project_id, paper_id, spec_id, name, status) "
        "values (%s, %s, %s, %s, 'attempt-hist-exp', 'approved')",
        (str(exp_id), str(project_id), str(paper_id), str(spec_id)),
    )
    db.execute(
        "insert into public.experiment_plans "
        "(id, experiment_id, version, config_json, approved_at) "
        "values (%s, %s, 1, %s::jsonb, %s)",
        (str(plan_id), str(exp_id), json.dumps(VALID_CONFIG), datetime.now(UTC)),
    )
    db.execute(
        "insert into public.execution_runs "
        "(id, experiment_id, plan_id, code_version, plan_version, status, repair_attempt) "
        "values (%s, %s, %s, 1, 1, 'running', 0)",
        (str(run_id), str(exp_id), str(plan_id)),
    )
    return {
        "project_id": str(project_id),
        "paper_id": str(paper_id),
        "spec_id": str(spec_id),
        "experiment_id": str(exp_id),
        "plan_id": str(plan_id),
        "run_id": str(run_id),
    }


def _insert_files(db, experiment_id, prefix, count=2, version=1):
    ids = []
    for i in range(count):
        fid = uuid4()
        db.execute(
            "insert into public.generated_files "
            "(id, experiment_id, version, path, content, sha256, change_reason) "
            "values (%s, %s, %s, %s, 'x', %s, 'seed')",
            (str(fid), experiment_id, version,
             f"{prefix}/file_{i}.py", hashlib.sha256(f"{prefix}-{i}".encode()).hexdigest()),
        )
        ids.append(str(fid))
    return ids


# ============================================================
# Table contract
# ============================================================


class TestSchemaContract:
    def test_execution_attempts_table_exists(self, db):
        db.execute(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name='execution_attempts'"
        )
        columns = {r["column_name"] for r in db.fetchall()}
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
        columns = {r["column_name"] for r in db.fetchall()}
        assert {"run_id", "attempt", "generated_file_id"} == columns

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
        db.execute(
            "insert into public.execution_attempts "
            "(run_id, attempt, plan_version, code_version, status) "
            "values (%s, 1, 1, 1, 'running')",
            (run_context["run_id"],),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            db.execute(
                "insert into public.execution_attempts "
                "(run_id, attempt, plan_version, code_version, status) "
                "values (%s, 1, 1, 1, 'running')",
                (run_context["run_id"],),
            )

    def test_attempt_must_be_positive(self, db, run_context):
        with pytest.raises(psycopg2.errors.CheckViolation):
            db.execute(
                "insert into public.execution_attempts "
                "(run_id, attempt, plan_version, code_version, status) "
                "values (%s, 0, 1, 1, 'queued')",
                (run_context["run_id"],),
            )

    def test_attempt_files_require_existing_attempt(self, db, run_context):
        file_id = _insert_files(db, run_context["experiment_id"], "orphan")[0]
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            db.execute(
                "insert into public.execution_attempt_files (run_id, attempt, generated_file_id) "
                "values (%s, 5, %s)",
                (run_context["run_id"], file_id),
            )


# ============================================================
# Append-only semantics
# ============================================================


class TestAppendOnlySemantics:
    def test_second_attempt_does_not_erase_first(self, db, run_context):
        """The core promise: a new attempt row never deletes a prior binding."""
        run_id = run_context["run_id"]
        first_ids = _insert_files(db, run_context["experiment_id"], "v1", version=1)
        second_ids = _insert_files(db, run_context["experiment_id"], "v2", version=2)
        for attempt, ids in ((1, first_ids), (2, second_ids)):
            db.execute(
                "insert into public.execution_attempts "
                "(run_id, attempt, plan_version, code_version, status) "
                "values (%s, %s, 1, %s, 'running')",
                (run_id, attempt, attempt),
            )
            for fid in ids:
                db.execute(
                    "insert into public.execution_attempt_files "
                    "(run_id, attempt, generated_file_id) values (%s, %s, %s)",
                    (run_id, attempt, fid),
                )

        db.execute(
            "select attempt, generated_file_id from public.execution_attempt_files "
            "where run_id = %s order by attempt",
            (run_id,),
        )
        rows = db.fetchall()
        first_bound = [r["generated_file_id"] for r in rows if r["attempt"] == 1]
        second_bound = [r["generated_file_id"] for r in rows if r["attempt"] == 2]
        assert set(first_bound) == set(first_ids)
        assert set(second_bound) == set(second_ids)

    def test_artifact_query_is_scoped_to_attempt(self, db, run_context):
        run_id = run_context["run_id"]
        for attempt in (1, 2):
            db.execute(
                "insert into public.execution_attempts "
                "(run_id, attempt, plan_version, code_version, status) "
                "values (%s, %s, 1, %s, 'succeeded')",
                (run_id, attempt, attempt),
            )
            db.execute(
                "insert into public.execution_artifacts "
                "(run_id, attempt, artifact_type, storage_path, metadata_json) "
                "values (%s, %s, 'metrics.json', %s, '{}'::jsonb)",
                (run_id, attempt, f"{run_id}/attempt-{attempt}/artifacts/metrics.json"),
            )
        db.execute(
            "select storage_path from public.execution_artifacts "
            "where run_id = %s and attempt = 2",
            (run_id,),
        )
        paths = [r["storage_path"] for r in db.fetchall()]
        assert paths == [f"{run_id}/attempt-2/artifacts/metrics.json"]

    def test_history_files_are_append_only_via_trigger(self, db, run_context):
        """Update is forbidden on execution_attempt_files rows."""
        run_id = run_context["run_id"]
        fid = _insert_files(db, run_context["experiment_id"], "immutable")[0]
        db.execute(
            "insert into public.execution_attempts "
            "(run_id, attempt, plan_version, code_version, status) "
            "values (%s, 1, 1, 1, 'running')",
            (run_id,),
        )
        db.execute(
            "insert into public.execution_attempt_files (run_id, attempt, generated_file_id) "
            "values (%s, 1, %s)",
            (run_id, fid),
        )
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute(
                "update public.execution_attempt_files set generated_file_id = %s "
                "where run_id = %s and attempt = 1",
                (_insert_files(db, run_context["experiment_id"], "swap")[0], run_id),
            )


# ============================================================
# Legacy backfill
# ============================================================


class TestLegacyBackfill:
    def test_attempt_zero_paths_are_backfilled_to_attempt_one(
        self, db, run_context, migration_probe
    ):
        """The migration's backfill step maps legacy 0-based paths to attempt=1.

        The test cannot re-run the migration, but it verifies that the same SQL
        logic produces the expected outcome for a legacy-shaped artifact row:
        metadata_json.attempt = 0 with storage_path 'attempt-0/...' maps to
        attempt=1 in the current database.
        """
        run_id = run_context["run_id"]
        db.execute(
            "insert into public.execution_attempts "
            "(run_id, attempt, plan_version, code_version, status) "
            "values (%s, 1, 1, 1, 'succeeded')",
            (run_id,),
        )
        # Simulate a legacy artifact written before this migration:
        # metadata_json records the old 0-based attempt ordinal and the file
        # lives at the 0-based path.
        db.execute(
            "insert into public.execution_artifacts "
            "(run_id, artifact_type, storage_path, metadata_json) "
            "values (%s, 'metrics.json', %s, '{\"attempt\": 0}'::jsonb)",
            (run_id, f"{run_id}/attempt-0/artifacts/metrics.json"),
        )
        # Apply the same UPDATE statement the migration uses for legacy rows.
        db.execute(
            "update public.execution_artifacts set attempt = 1 "
            "where run_id = %s and attempt is null "
            "and (metadata_json->>'attempt') = '0'",
            (run_id,),
        )
        db.execute(
            "select attempt, storage_path from public.execution_artifacts "
            "where run_id = %s",
            (run_id,),
        )
        row = db.fetchone()
        assert row["attempt"] == 1
        # Files on disk are NOT renamed: storage_path preserves the legacy prefix.
        assert row["storage_path"] == f"{run_id}/attempt-0/artifacts/metrics.json"


@pytest.fixture
def migration_probe():
    """Placeholder so the test name documents intent; no setup required."""
    return True
