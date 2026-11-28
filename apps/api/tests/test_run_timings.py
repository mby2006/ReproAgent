"""Per-attempt segment persistence and owner-checked timing aggregation."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from reproagent_api.services import runs as runs_service
from reproagent_schemas import RunAudit, RunResult, RunStatus
from test_runs import (
    AUTH,
    BOB_AUTH,
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    FakeRPC,
    _seed_run,
    seed_tables,
)
from test_runs import (
    env as env,
)

RESOURCE_BUDGET = {
    "max_seconds": 321,
    "memory_mb": 768,
    "cpu_count": 1.5,
    "pids_limit": 128,
    "disk_mb": 512,
    "device": "cpu",
}


def seed_attempt(
    admin,
    run_id,
    attempt,
    *,
    status="succeeded",
    accepted_at="2026-01-01T00:00:02+00:00",
    timing_json=None,
):
    row = {
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": attempt,
        "plan_id": admin.tables["execution_runs"][0]["plan_id"],
        "plan_version": 1,
        "code_version": attempt,
        "status": status,
        "runner_request_id": str(uuid4()),
        "runner_run_id": str(uuid4()),
        "dispatch_started_at": "2026-01-01T00:00:00+00:00",
        "runner_accepted_at": accepted_at,
        "finished_at": "2026-01-01T00:00:03+00:00" if status in {
            "succeeded", "failed", "timeout", "cancelled"
        } else None,
        "timing_json": timing_json or {},
    }
    admin.tables["execution_attempts"].append(row)
    return row


def seed_report(admin, run_id, attempt, version, generation_ms):
    admin.tables["reproduction_reports"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": attempt,
        "version": version,
        "generation_ms": generation_ms,
        "content_sha256": hashlib.sha256(f"{attempt}:{version}".encode()).hexdigest(),
        "report_json": {"references": {"execution_attempt": attempt}},
        "markdown": f"attempt {attempt} version {version}",
        "created_at": "2026-01-01T00:00:00+00:00",
    })


def timing_environment(admin, run_id):
    plan = admin.tables["experiment_plans"][0]
    plan_hash = hashlib.sha256(
        json.dumps(
            plan["config_json"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    return {
        "image_digest": "sha256:image",
        "dataset_fingerprint": "dataset-v1",
        "dependency_lock_hash": "deps-v1",
        "plan_hash": plan_hash,
        "code_hash": "code-v1",
        "seed": 42,
        "resource_budget": dict(RESOURCE_BUDGET),
    }


def test_timings_endpoint_orders_attempts_and_uses_latest_report_per_attempt(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["execution_runs"][0]["created_at"] = "2026-01-01T00:00:00+00:00"
    environment = timing_environment(admin, run_id)
    seed_attempt(
        admin,
        run_id,
        2,
        accepted_at="2026-01-01T00:00:02+00:00",
        timing_json={
            "runner_queue_seconds": 0.25,
            "container_setup_seconds": 1.0,
            "container_lifecycle_seconds": 6.0,
            "training_evaluation_seconds": 2.0,
            "container_cleanup_seconds": 3.0,
            "runner_total_seconds": 7.0,
            "report_generation_seconds": 99.0,
            "environment": environment,
        },
    )
    seed_attempt(
        admin,
        run_id,
        1,
        status="failed",
        accepted_at="2026-01-01T00:00:01+00:00",
        timing_json={
            "container_setup_seconds": 0.1,
            "container_lifecycle_seconds": 0.3,
            "training_evaluation_seconds": 0.1,
            "container_cleanup_seconds": 0.1,
            "environment": environment,
        },
    )
    seed_report(admin, run_id, 1, 1, 100)
    seed_report(admin, run_id, 1, 2, 250)
    seed_report(admin, run_id, 2, 3, 400)

    response = client.get(f"/v1/runs/{run_id}/timings", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert [row["attempt"] for row in body] == [1, 2]
    assert body[0] == {
        "run_id": run_id,
        "attempt": 1,
        "status": "failed",
        "queue_seconds": 1.0,
        "container_lifecycle_seconds": 0.1 + 0.1 + 0.1,
        "training_evaluation_seconds": 0.1,
        "report_generation_seconds": 0.25,
        "total_seconds": 1.0 + (0.1 + 0.1 + 0.1) + 0.25,
        "environment": environment,
    }
    assert body[1]["queue_seconds"] == 2.0
    assert body[1]["container_lifecycle_seconds"] == 6.0
    assert body[1]["training_evaluation_seconds"] == 2.0
    assert body[1]["report_generation_seconds"] == 0.4
    assert body[1]["total_seconds"] == 8.4
    assert body[1]["environment"]["resource_budget"] == RESOURCE_BUDGET
    assert client.get(f"/v1/runs/{run_id}/timings", headers=BOB_AUTH).status_code == 404


def test_timings_endpoint_keeps_missing_stages_and_total_none(env):
    client, admin, _ = env
    run_id = _seed_run(admin, status="running")
    seed_attempt(admin, run_id, 1, status="running", accepted_at=None, timing_json={})

    response = client.get(f"/v1/runs/{run_id}/timings", headers=AUTH)

    assert response.status_code == 200
    row = response.json()[0]
    assert row["queue_seconds"] is None
    assert row["container_lifecycle_seconds"] is None
    assert row["training_evaluation_seconds"] is None
    assert row["report_generation_seconds"] is None
    assert row["total_seconds"] is None
    assert row["environment"] == {}


def test_timings_endpoint_returns_empty_list_without_attempts(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    assert client.get(f"/v1/runs/{run_id}/timings", headers=AUTH).json() == []


@pytest.mark.asyncio
async def test_result_persistence_merges_runner_timings_and_full_environment_after_rpc_success():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin, status="running")
    run = admin.tables["execution_runs"][0]
    run["created_at"] = "2026-01-01T00:00:00+00:00"
    attempt = seed_attempt(admin, run_id, 1, status="running")
    result = RunResult(
        run_id=uuid4(),
        status=RunStatus.SUCCEEDED,
        audit={
            "image_digest": "sha256:image",
            "dataset_fingerprint": "dataset-v1",
            "dependency_lock_hash": "deps-v1",
            "code_hash": "code-v1",
            "seed": 42,
            "resource_budget": RESOURCE_BUDGET,
        },
        timings={
            "runner_queue_seconds": 0.25,
            "container_setup_seconds": 1.0,
            "training_evaluation_seconds": 2.0,
            "container_cleanup_seconds": 3.0,
            "container_lifecycle_seconds": 6.0,
            "runner_total_seconds": 7.0,
        },
    )

    class ArtifactRunner:
        async def get_artifact_bytes(self, run_id, name):
            return b"artifact"

    await runs_service.async_persist_result(
        admin, run_id, result.run_id, result, ArtifactRunner(), 0
    )

    timing = attempt["timing_json"]
    assert timing["runner_queue_seconds"] == 0.25
    assert timing["runner_total_seconds"] == 7.0
    assert timing["container_setup_seconds"] == 1.0
    assert timing["training_evaluation_seconds"] == 2.0
    assert timing["container_cleanup_seconds"] == 3.0
    assert timing["container_lifecycle_seconds"] == 6.0
    assert timing["queue_seconds"] == 2.0
    assert timing["report_generation_seconds"] is None
    assert timing["total_seconds"] is None
    assert timing["environment"] == timing_environment(admin, run_id)
    assert timing["environment"]["resource_budget"] == RESOURCE_BUDGET


@pytest.mark.asyncio
async def test_result_persistence_does_not_write_timings_when_atomic_rpc_returns_false():
    class RejectingAdmin(FakeAdmin):
        def _rpc(self, name, params):
            if name == "persist_execution_result":
                self.rpc_calls.append((name, params))
                return FakeRPC(False)
            return super()._rpc(name, params)

    admin = RejectingAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin, status="running")
    attempt = seed_attempt(
        admin, run_id, 1, status="running", timing_json={"environment": {"protected": True}}
    )
    result = RunResult(
        run_id=uuid4(), status=RunStatus.SUCCEEDED, timings={"runner_total_seconds": 7.0}
    )

    class ArtifactRunner:
        async def get_artifact_bytes(self, run_id, name):
            return b"artifact"

    await runs_service.async_persist_result(
        admin, run_id, result.run_id, result, ArtifactRunner(), 0
    )

    assert attempt["timing_json"] == {"environment": {"protected": True}}


@pytest.mark.asyncio
async def test_result_persistence_never_mutates_terminal_attempt_even_after_rpc_success():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin, status="running")
    attempt = seed_attempt(
        admin, run_id, 1, status="succeeded", timing_json={"environment": {"protected": True}}
    )
    result = RunResult(
        run_id=uuid4(), status=RunStatus.SUCCEEDED, timings={"runner_total_seconds": 7.0}
    )

    class ArtifactRunner:
        async def get_artifact_bytes(self, run_id, name):
            return b"artifact"

    await runs_service.async_persist_result(
        admin, run_id, result.run_id, result, ArtifactRunner(), 0
    )

    assert attempt["timing_json"] == {"environment": {"protected": True}}


def test_runner_acceptance_stamps_nonterminal_attempt():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin, status="running")
    attempt = seed_attempt(admin, run_id, 1, status="running", accepted_at=None)
    before = datetime.now(UTC)

    assert runs_service.persist_runner_run_id(admin, run_id, uuid4(), 0) is True

    accepted = datetime.fromisoformat(attempt["runner_accepted_at"])
    assert before <= accepted <= datetime.now(UTC)
    assert attempt["status"] == "running"


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "timeout", "cancelled"])
def test_runner_acceptance_never_mutates_terminal_attempt(terminal):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin, status="running")
    attempt = seed_attempt(admin, run_id, 1, status=terminal, accepted_at=None)
    original = dict(attempt)

    runs_service.persist_runner_run_id(admin, run_id, uuid4(), 0)

    assert attempt == original


def test_run_audit_carries_complete_resource_budget():
    audit = RunAudit(resource_budget=RESOURCE_BUDGET)
    assert audit.resource_budget.model_dump(mode="json") == RESOURCE_BUDGET
