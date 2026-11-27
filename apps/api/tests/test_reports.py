"""Report versions are append-only and evaluate only the final attempt's evidence."""

import copy
import hashlib
import json
from uuid import uuid4

import pytest
from reproagent_agent.evaluation import evaluate_run
from reproagent_api.services import reports
from reproagent_schemas import ExperimentPlan, RunStatus
from test_runs import (
    AUTH,
    BOB_AUTH,
    EXPERIMENT_ID,
    PLAN_ID,
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    _seed_run,
    seed_tables,
)
from test_runs import env as env


def _plan_with_batch(batch_size):
    raw = ExperimentPlan.model_validate(
        seed_tables()["experiment_plans"][0]["config_json"]
    ).model_dump(mode="json")
    raw["training_config"]["batch_size"]["value"] = batch_size
    return raw


def _final_attempt_admin():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin)
    run = admin.tables["execution_runs"][0]
    run.update(plan_id=str(PLAN_ID), code_version=1, metrics_json={"metric_value": 0.10})

    second_plan_id = str(uuid4())
    admin.tables["experiment_plans"].append({
        "id": second_plan_id,
        "experiment_id": str(EXPERIMENT_ID),
        "version": 2,
        "config_json": _plan_with_batch(8),
        "approved_at": "2026-01-01T00:00:00+00:00",
    })
    file_ids = []
    for attempt, prefix in ((1, "first"), (2, "final")):
        attempt_files = []
        for index, name in enumerate(("config.yaml", "train.py")):
            file_id = f"00000000-0000-0000-0000-{attempt * 10 + index:012d}"
            content = f"{prefix}-{name}"
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            admin.tables["generated_files"].append({
                "id": file_id,
                "experiment_id": str(EXPERIMENT_ID),
                "version": attempt,
                "path": name,
                "content": content,
                "sha256": sha256,
            })
            admin.tables["execution_attempt_files"].append({
                "run_id": run_id, "attempt": attempt, "generated_file_id": file_id,
            })
            attempt_files.append(file_id)
        request_id = uuid4()
        runner_run_id = uuid4()
        admin.tables["execution_attempts"].append({
            "id": str(uuid4()),
            "run_id": run_id,
            "attempt": attempt,
            "plan_id": str(PLAN_ID) if attempt == 1 else second_plan_id,
            "plan_version": 1 if attempt == 1 else 2,
            "code_version": attempt,
            "status": "failed" if attempt == 1 else "succeeded",
            "runner_request_id": str(request_id),
            "runner_run_id": str(runner_run_id),
            "files_pinned": True,
        })
        metric_value = 0.10 if attempt == 1 else 0.90
        path = f"{run_id}/attempt-{attempt}/artifacts/metrics.json"
        admin.tables["execution_artifacts"].append({
            "id": str(uuid4()),
            "run_id": run_id,
            "attempt": attempt,
            "artifact_type": "metrics.json",
            "storage_path": path,
            "metadata_json": {"attempt": attempt},
        })
        admin.storage.files[path] = json.dumps({"metric_value": metric_value}).encode()
        if attempt == 2:
            file_ids = attempt_files
            final_request_id = request_id
            final_runner_run_id = runner_run_id
    admin.tables["execution_artifacts"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": 2,
        "artifact_type": "model.pt",
        "storage_path": f"{run_id}/attempt-2/artifacts/model.pt",
        "metadata_json": {"attempt": 2},
    })
    workflow_id = str(uuid4())
    admin.tables["workflows"].append({
        "id": workflow_id,
        "run_id": run_id,
        "project_id": str(PROJECT_ID),
        "paper_id": admin.tables["papers"][0]["id"],
        "status": "completed",
        "thread_id": workflow_id,
    })
    return admin, run_id, {
        "file_ids": file_ids,
        "plan_id": second_plan_id,
        "request_id": final_request_id,
        "runner_run_id": final_runner_run_id,
        "workflow_id": workflow_id,
    }


def test_evaluate_request_uses_only_final_attempt_plan_code_and_artifacts():
    admin, run_id, expected = _final_attempt_admin()

    request = reports.build_evaluate_request(admin, run_id)

    assert request.metrics == {"metric_value": 0.90}
    assert request.plan.training_config.batch_size.value == 8
    assert sorted(file.path for file in request.code_files) == ["config.yaml", "train.py"]
    assert all(file.content.startswith("final-") for file in request.code_files)
    assert request.artifacts == ["metrics.json", "model.pt"]
    refs = request.references
    assert str(refs.plan_id) == expected["plan_id"]
    assert refs.plan_version == 2 and refs.code_version == 2
    assert {str(value) for value in refs.generated_file_ids} == set(expected["file_ids"])
    assert refs.execution_attempt == 2
    assert str(refs.execution_request_id) == str(expected["request_id"])
    assert refs.runner_run_id == str(expected["runner_run_id"])
    assert str(refs.workflow_id) == expected["workflow_id"]
    expected_hash = hashlib.sha256((
        "config.yaml:"
        + hashlib.sha256(b"final-config.yaml").hexdigest()
        + "\ntrain.py:"
        + hashlib.sha256(b"final-train.py").hexdigest()
    ).encode()).hexdigest()
    assert refs.code_hash == expected_hash


def test_shuffled_database_rows_produce_identical_evaluation_and_report_hash():
    admin, run_id, _ = _final_attempt_admin()
    shuffled = copy.deepcopy(admin)
    for table in ("execution_attempt_files", "generated_files", "execution_artifacts"):
        shuffled.tables[table].reverse()

    original_request = reports.build_evaluate_request(admin, run_id)
    shuffled_request = reports.build_evaluate_request(shuffled, run_id)

    assert shuffled_request.model_dump(mode="json") == original_request.model_dump(mode="json")
    assert [file.path for file in shuffled_request.code_files] == [
        "config.yaml", "train.py",
    ]
    assert [str(value) for value in shuffled_request.references.generated_file_ids] == sorted(
        str(value) for value in original_request.references.generated_file_ids
    )
    assert shuffled_request.artifacts == ["metrics.json", "model.pt"]

    original = evaluate_run(original_request)
    reordered = evaluate_run(shuffled_request)
    assert reordered.report == original.report
    assert reordered.markdown == original.markdown
    original_version = reports.append_report_version(
        admin,
        run_id,
        original.report.model_dump(mode="json"),
        original.markdown,
    )
    shuffled_version = reports.append_report_version(
        shuffled,
        run_id,
        reordered.report.model_dump(mode="json"),
        reordered.markdown,
    )
    assert shuffled_version.report_json == original_version.report_json
    assert shuffled_version.markdown == original_version.markdown
    assert shuffled_version.content_sha256 == original_version.content_sha256


def test_missing_final_metrics_artifact_never_falls_back_to_another_attempt():
    admin, run_id, _ = _final_attempt_admin()
    admin.tables["execution_artifacts"] = [
        row for row in admin.tables["execution_artifacts"]
        if not (row["attempt"] == 2 and row["artifact_type"] == "metrics.json")
    ]

    request = reports.build_evaluate_request(admin, run_id)

    assert request.metrics == {}
    assert request.artifacts == ["model.pt"]


def test_pre_attempt_report_uses_legacy_projection_without_faking_attempt_one():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin)
    file_id = str(uuid4())
    admin.tables["generated_files"].append({
        "id": file_id,
        "experiment_id": str(EXPERIMENT_ID),
        "version": 1,
        "path": "train.py",
        "content": "legacy",
        "sha256": hashlib.sha256(b"legacy").hexdigest(),
    })
    admin.tables["execution_run_files"].append({
        "run_id": run_id, "generated_file_id": file_id,
    })
    admin.tables["execution_artifacts"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": None,
        "artifact_type": "metrics.json",
        "storage_path": "legacy/metrics.json",
    })

    request = reports.build_evaluate_request(admin, run_id)

    assert request.references.execution_attempt is None
    assert request.references.execution_request_id is None
    assert request.references.runner_run_id is None
    assert [str(value) for value in request.references.generated_file_ids] == [file_id]
    assert request.metrics == {"accuracy": 0.98}
    assert request.artifacts == ["metrics.json"]


def test_append_report_version_returns_existing_version_for_identical_content():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin)

    first = reports.append_report_version(admin, run_id, {"revision": 1}, "first", 12.5)
    duplicate = reports.append_report_version(
        admin, run_id, {"revision": 1}, "first", generation_ms=99
    )
    second = reports.append_report_version(admin, run_id, {"revision": 2}, "second", 25)

    assert first.version == 1 and second.version == 2
    assert duplicate.id == first.id and duplicate.version == 1
    assert first.generation_ms == 12.5 and second.generation_ms == 25
    # The real SQL migration/integration test verifies the exact pgcrypto digest;
    # this fake models the same 64-character content identity and idempotency.
    assert len(first.content_sha256) == 64
    assert all(char in "0123456789abcdef" for char in first.content_sha256)
    assert reports.get_report(admin, run_id).version == 2
    assert reports.get_report(admin, run_id, version=1).markdown == "first"


@pytest.mark.asyncio
async def test_manual_report_generation_appends_and_records_real_elapsed_time(monkeypatch):
    admin, run_id, _ = _final_attempt_admin()
    ticks = iter((10.0, 10.25))
    monkeypatch.setattr(reports.time, "perf_counter", lambda: next(ticks))

    class DeterministicAgent:
        async def evaluate(self, request):
            return evaluate_run(request)

    version = await reports.generate_and_store_report(admin, DeterministicAgent(), run_id)

    assert version.version == 1
    assert version.generation_ms == 250
    assert version.report_json["references"]["execution_attempt"] == 2
    assert version.report_json["references"]["plan_version"] == 2


def _report_row(run_id, version, markdown):
    return {
        "id": str(uuid4()),
        "run_id": run_id,
        "version": version,
        "attempt": 2,
        "generation_ms": 10,
        "content_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
        "report_json": {"references": {"run_id": run_id}, "markdown": markdown},
        "markdown": markdown,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_get_report_defaults_to_latest_and_selects_an_older_version(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["reproduction_reports"] = [
        _report_row(run_id, 1, "old"),
        _report_row(run_id, 2, "new"),
    ]

    latest = client.get(f"/v1/runs/{run_id}/report", headers=AUTH)
    older = client.get(f"/v1/runs/{run_id}/report?version=1", headers=AUTH)

    assert latest.status_code == 200
    assert latest.json()["version"] == 2 and latest.json()["markdown"] == "new"
    assert latest.json()["execution_attempt"] == 2
    assert latest.json()["generated_at"] == "2026-01-01T00:00:00Z"
    assert older.status_code == 200
    assert older.json()["version"] == 1 and older.json()["markdown"] == "old"


@pytest.mark.parametrize("version", ["0", "-1", "not-a-number", "1.5"])
def test_get_report_rejects_invalid_versions(env, version):
    client, admin, _ = env
    run_id = _seed_run(admin)

    response = client.get(f"/v1/runs/{run_id}/report?version={version}", headers=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_REPORT_VERSION"


def test_get_report_missing_version_is_distinct_from_missing_report(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["reproduction_reports"] = [_report_row(run_id, 1, "old")]

    response = client.get(f"/v1/runs/{run_id}/report?version=2", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REPORT_VERSION_NOT_FOUND"


def test_get_report_keeps_ownership_check_for_versioned_history(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["reproduction_reports"] = [_report_row(run_id, 1, "private")]

    assert client.get(f"/v1/runs/{run_id}/report?version=1", headers=BOB_AUTH).status_code == 404


def test_get_report_reports_confirmed_generation_failure(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["workflows"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "status": "failed",
        "current_node": "evaluate_result",
        "error": "evaluator unavailable",
    })

    response = client.get(f"/v1/runs/{run_id}/report", headers=AUTH)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REPORT_GENERATION_FAILED"


def test_run_status_remains_truthful_when_report_is_unavailable(env):
    client, admin, _ = env
    run_id = _seed_run(admin, status="failed")

    response = client.get(f"/v1/runs/{run_id}/report", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REPORT_UNAVAILABLE"
    assert admin.tables["execution_runs"][0]["status"] == RunStatus.FAILED.value
