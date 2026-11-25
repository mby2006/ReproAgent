"""Offline regressions for durable, duplicate-safe execution-run dispatch."""

import asyncio
from importlib import import_module
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from reproagent_api import worker
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import make_memory_checkpointer
from reproagent_schemas import RunAccepted, RunStatus, WorkflowStatus
from test_runs import (
    AUTH,
    EXPERIMENT_ID,
    PLAN_ID,
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    FakeArq,
    FakeQuery,
    _always_acquired,
    seed_tables,
)

pytest_plugins = ["test_runs"]

STALE = "2020-01-01T00:00:00+00:00"
FRESH = "2999-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def offline_drive_lock(monkeypatch):
    """Every direct run_experiment_task call must avoid a real advisory lock."""
    monkeypatch.setattr(worker, "_drive_lock", _always_acquired)


def _admin():
    return FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})


def _ctx(admin, monkeypatch, redis=None):
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    return {
        "settings": settings,
        "agent_transport": None,
        "runner_transport": None,
        "checkpointer": make_memory_checkpointer(),
        "redis": redis,
    }


def _run(
    admin, status="queued", updated_at=STALE, *, run_id=None,
    created_at="2026-01-01T00:00:00+00:00",
):
    row = {
        "experiment_id": str(EXPERIMENT_ID),
        "plan_id": str(PLAN_ID),
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    if run_id is not None:
        row["id"] = run_id
    return admin.table("execution_runs").insert(row).execute().data[0]


def _outbox(admin, entity_id, status="pending"):
    return admin.table("job_outbox").insert({
        "job_type": "run_experiment_task",
        "entity_id": entity_id,
        "job_id": f"run-experiment:{entity_id}",
        "status": status,
    }).execute().data[0]


def _workflow(
    admin, run, status=WorkflowStatus.EXECUTING.value, error=None, workflow_id=None
):
    workflow_id = workflow_id or str(uuid4())
    return admin.table("workflows").insert({
        "id": workflow_id,
        "thread_id": workflow_id,
        "project_id": str(PROJECT_ID),
        "paper_id": str(uuid4()),
        "experiment_id": str(EXPERIMENT_ID),
        "run_id": run["id"],
        "status": status,
        "error": error,
    }).execute().data[0]


def _dispatch():
    return import_module("reproagent_api.services.dispatch")


def test_dispatching_is_a_public_run_status():
    accepted = RunAccepted(run_id=uuid4(), status="dispatching")
    assert accepted.status is RunStatus.DISPATCHING


def test_start_run_uses_atomic_rpc_and_stable_outbox_dispatch(env):
    client, admin, app = env
    response = client.post(f"/v1/experiments/{EXPERIMENT_ID}/runs", headers=AUTH)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert admin.rpc_calls == [("create_execution_run", {
        "p_experiment_id": str(EXPERIMENT_ID),
        "p_plan_id": str(PLAN_ID),
        "p_plan_version": 1,
    })]
    outbox = admin.tables["job_outbox"][0]
    assert outbox["entity_id"] == run_id
    assert outbox["job_id"] == f"run-experiment:{run_id}"
    assert outbox["status"] == "enqueued"
    assert app.state.arq.enqueued == [
        ("run_experiment_task", (run_id,), f"run-experiment:{run_id}")
    ]


def test_redis_outage_keeps_202_and_pending_outbox_then_retries(env):
    client, admin, app = env

    class OutageArq:
        async def enqueue_job(self, name, *args, _job_id=None):
            raise ConnectionError("redis unavailable")

        async def aclose(self):
            return None

    app.state.arq = OutageArq()
    response = client.post(f"/v1/experiments/{EXPERIMENT_ID}/runs", headers=AUTH)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "queued"
    assert admin.tables["execution_runs"][0]["status"] == "queued"
    assert admin.tables["experiments"][0]["status"] == "running"
    pending = admin.tables["job_outbox"][0]
    assert pending["status"] == "pending"
    assert pending["attempts"] == 1
    assert "redis unavailable" in pending["last_error"]

    app.state.arq = FakeArq()
    dispatched = asyncio.run(_dispatch().enqueue_pending_outbox(
        admin, app.state.arq, entity_id=run_id
    ))
    assert dispatched == 1
    assert pending["status"] == "enqueued"
    assert app.state.arq.enqueued == [
        ("run_experiment_task", (run_id,), f"run-experiment:{run_id}")
    ]


def test_dispatch_continues_after_one_enqueue_failure():
    admin = _admin()
    first = _outbox(admin, str(uuid4()))
    second = _outbox(admin, str(uuid4()))

    class PartiallyFailingArq(FakeArq):
        async def enqueue_job(self, name, *args, _job_id=None):
            if args[0] == first["entity_id"]:
                raise ConnectionError("first enqueue failed")
            return await super().enqueue_job(name, *args, _job_id=_job_id)

    redis = PartiallyFailingArq()
    assert asyncio.run(_dispatch().enqueue_pending_outbox(admin, redis)) == 1
    assert first["status"] == "pending"
    assert first["attempts"] == 1
    assert "first enqueue failed" in first["last_error"]
    assert second["status"] == "enqueued"
    assert redis.enqueued == [
        ("run_experiment_task", (second["entity_id"],), second["job_id"])
    ]


def test_dispatch_without_redis_does_not_mutate_outbox():
    admin = _admin()
    row = _outbox(admin, str(uuid4()))
    before = dict(row)
    assert asyncio.run(_dispatch().enqueue_pending_outbox(admin, None)) == 0
    assert row == before


def test_duplicate_delivery_claims_once_and_drives_one_deterministic_workflow(monkeypatch):
    admin = _admin()
    run = _run(admin)
    drives = []

    async def drive(wctx, checkpointer, state):
        drives.append(state["run_id"])
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "drive_from_execution", drive)
    ctx = _ctx(admin, monkeypatch)

    async def deliver_twice():
        await asyncio.gather(
            worker.run_experiment_task(ctx, run["id"]),
            worker.run_experiment_task(ctx, run["id"]),
        )

    asyncio.run(deliver_twice())
    expected_id = str(uuid5(NAMESPACE_URL, f"reproagent:run:{run['id']}"))
    assert drives == [run["id"]]
    assert len(admin.tables["workflows"]) == 1
    workflow = admin.tables["workflows"][0]
    assert workflow["id"] == expected_id
    assert workflow["thread_id"] == expected_id
    assert workflow["run_id"] == run["id"]


def test_existing_workflow_discovered_after_claim_is_recovered_without_insert(monkeypatch):
    admin = _admin()
    run = _run(admin)
    workflow = _workflow(admin, run)
    redis = FakeArq()

    async def unexpected_drive(*args):
        raise AssertionError("an existing workflow must not be inserted or driven again")

    monkeypatch.setattr(worker, "drive_from_execution", unexpected_drive)
    asyncio.run(worker.run_experiment_task(_ctx(admin, monkeypatch, redis), run["id"]))

    assert admin.tables["workflows"] == [workflow]
    assert run["status"] == "dispatching"
    assert redis.enqueued == [
        ("recover_workflow_task", (workflow["id"],), None)
    ]


def test_workflow_created_after_claim_lookup_is_not_duplicated(monkeypatch):
    admin = _admin()
    run = _run(admin)
    redis = FakeArq()
    expected_id = str(uuid5(NAMESPACE_URL, f"reproagent:run:{run['id']}"))
    upserts = []
    upsert = FakeQuery.upsert

    def record_upsert(query, row, *, on_conflict=None, ignore_duplicates=False):
        if query._name == "workflows":
            upserts.append((on_conflict, row["id"], ignore_duplicates))
        return upsert(
            query, row, on_conflict=on_conflict, ignore_duplicates=ignore_duplicates
        )

    monkeypatch.setattr(FakeQuery, "upsert", record_upsert)
    recover = worker._recover_run_workflow
    workflow = []

    async def create_after_lookup(ctx, client, claimed_run):
        found = await recover(ctx, client, claimed_run)
        if not workflow:
            workflow.append(_workflow(admin, run, workflow_id=expected_id))
        return found

    async def unexpected_drive(*args):
        raise AssertionError("a racing workflow must be recovered, not duplicated")

    monkeypatch.setattr(worker, "_recover_run_workflow", create_after_lookup)
    monkeypatch.setattr(worker, "drive_from_execution", unexpected_drive)
    asyncio.run(worker.run_experiment_task(_ctx(admin, monkeypatch, redis), run["id"]))

    assert upserts == [("id", expected_id, True)]
    assert admin.tables["workflows"] == [workflow[0]]
    assert redis.enqueued == [
        ("recover_workflow_task", (workflow[0]["id"],), None)
    ]


def test_dispatching_run_is_not_driven_again(monkeypatch):
    admin = _admin()
    run = _run(admin, status="dispatching", updated_at=FRESH)

    async def unexpected_drive(*args):
        raise AssertionError("a claimed run must not be driven twice")

    monkeypatch.setattr(worker, "drive_from_execution", unexpected_drive)
    asyncio.run(worker.run_experiment_task(_ctx(admin, monkeypatch), run["id"]))
    assert admin.tables["workflows"] == []


@pytest.mark.parametrize(("task_name", "args"), [
    ("run_experiment_task", ("missing-run",)),
    ("run_workflow_task", ("missing-workflow",)),
    ("resume_workflow_task", ("missing-workflow", "spec")),
    ("recover_workflow_task", ("missing-workflow",)),
])
def test_missing_run_or_workflow_raises_visible_error(monkeypatch, task_name, args):
    admin = _admin()
    task = getattr(worker, task_name)
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(task(_ctx(admin, monkeypatch), *args))


def test_authorization_failure_only_fails_the_claimed_dispatching_row(monkeypatch):
    admin = _admin()
    run = _run(admin)

    def invalid_authority(*args):
        run["status"] = "running"
        raise ValueError("authority revoked")

    monkeypatch.setattr(worker, "load_execution_plan", invalid_authority)
    asyncio.run(worker.run_experiment_task(_ctx(admin, monkeypatch), run["id"]))
    assert run["status"] == "running"
    assert run.get("error") is None
    assert admin.tables["workflows"] == []


def test_reconciler_requeues_stale_dispatching_without_workflow(monkeypatch):
    admin = _admin()
    run = _run(admin, status="dispatching")
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert run["status"] == "queued"
    assert len(admin.tables["job_outbox"]) == 1
    outbox = admin.tables["job_outbox"][0]
    assert outbox["job_id"] == f"run-experiment:{run['id']}"
    assert outbox["status"] == "enqueued"
    assert redis.enqueued == [
        ("run_experiment_task", (run["id"],), f"run-experiment:{run['id']}")
    ]


def test_reconciler_recreates_missing_outbox_for_stale_queued_run(monkeypatch):
    admin = _admin()
    run = _run(admin)
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert run["status"] == "queued"
    assert admin.tables["job_outbox"][0]["status"] == "enqueued"
    assert redis.jobs == [("run_experiment_task", (run["id"],))]


def test_reconciler_recovers_stale_queued_run_with_existing_workflow(monkeypatch):
    admin = _admin()
    run = _run(admin)
    workflow = _workflow(admin, run)
    redis = FakeArq()

    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))

    assert run["status"] == "queued"
    assert admin.tables["job_outbox"] == []
    assert redis.enqueued == [
        ("recover_workflow_task", (workflow["id"],), None)
    ]


def test_reconciler_reenqueues_recovery_on_every_pass(monkeypatch):
    admin = _admin()
    run = _run(admin, status="running")
    workflow = _workflow(admin, run)
    redis = FakeArq()
    ctx = _ctx(admin, monkeypatch, redis)

    asyncio.run(worker.reconcile_dispatch_task(ctx))
    asyncio.run(worker.reconcile_dispatch_task(ctx))

    assert redis.enqueued == [
        ("recover_workflow_task", (workflow["id"],), None),
        ("recover_workflow_task", (workflow["id"],), None),
    ]


def test_reconciler_does_not_requeue_workflow_created_after_snapshot(monkeypatch):
    admin = _admin()
    first_run = _run(admin, status="dispatching")
    first_workflow = _workflow(admin, first_run)
    run = _run(admin, status="dispatching")
    workflow = []

    class RecoveryArq(FakeArq):
        async def enqueue_job(self, name, *args, _job_id=None):
            if not workflow:
                workflow.append(_workflow(admin, run))
            return await super().enqueue_job(name, *args, _job_id=_job_id)

    redis = RecoveryArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))

    assert run["status"] == "dispatching"
    assert admin.tables["job_outbox"] == []
    assert redis.enqueued == [
        (
            "recover_workflow_task",
            (first_workflow["id"],),
            None,
        ),
        ("recover_workflow_task", (workflow[0]["id"],), None),
    ]


@pytest.mark.parametrize(("workflow_status", "expected_run_status", "error"), [
    (WorkflowStatus.COMPLETED.value, "succeeded", None),
    (WorkflowStatus.FAILED.value, "failed", "sandbox execution failed"),
])
def test_reconciler_finalizes_run_from_terminal_workflow(
    monkeypatch, workflow_status, expected_run_status, error
):
    admin = _admin()
    run = _run(admin, status="running")
    _workflow(admin, run, status=workflow_status, error=error)

    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch)))

    assert run["status"] == expected_run_status
    assert run["finished_at"]
    assert run.get("error") == error
    assert admin.tables["experiments"][0]["status"] == expected_run_status
    assert admin.rpc_calls == [("reconcile_execution_run", {
        "p_run_id": run["id"],
        "p_status": expected_run_status,
        "p_error": error,
    })]


def test_reconciler_preserves_cancelled_workflow_without_requeueing(monkeypatch):
    admin = _admin()
    run = _run(admin, status="running")
    _workflow(
        admin, run, status=WorkflowStatus.CANCELLED.value, error="workflow cancelled"
    )
    redis = FakeArq()

    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))

    assert run["status"] == "cancelled"
    assert run["error"] == "workflow cancelled"
    assert admin.tables["experiments"][0]["status"] == "cancelled"
    assert redis.jobs == []
    assert admin.rpc_calls == [("reconcile_execution_run", {
        "p_run_id": run["id"],
        "p_status": "cancelled",
        "p_error": "workflow cancelled",
        "p_repair_attempt": 0,
        "p_require_latest": False,
    })]


@pytest.mark.parametrize(("newer_status", "newer_created_at", "expected_status"), [
    ("running", "2026-01-02T00:00:00+00:00", "running"),
    ("succeeded", "2026-01-01T00:00:00+00:00", "succeeded"),
])
def test_stale_terminal_workflow_does_not_overwrite_newer_run_experiment(
    monkeypatch, newer_status, newer_created_at, expected_status
):
    admin = _admin()
    stale_run = _run(
        admin, status="running", run_id="00000000-0000-0000-0000-000000000001"
    )
    _workflow(
        admin, stale_run, status=WorkflowStatus.FAILED.value, error="sandbox execution failed"
    )
    newer_run = _run(
        admin,
        status=newer_status,
        updated_at=FRESH,
        run_id="00000000-0000-0000-0000-000000000002",
        created_at=newer_created_at,
    )
    admin.tables["experiments"][0]["status"] = newer_status

    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch)))

    assert stale_run["status"] == "failed"
    assert stale_run["finished_at"]
    assert stale_run["error"] == "sandbox execution failed"
    assert newer_run["status"] == newer_status
    assert admin.tables["experiments"][0]["status"] == expected_status
    assert admin.rpc_calls == [("reconcile_execution_run", {
        "p_run_id": stale_run["id"],
        "p_status": "failed",
        "p_error": "sandbox execution failed",
    })]


@pytest.mark.parametrize("terminal_status", ["cancelled", "succeeded", "failed", "timeout"])
def test_reconciler_preserves_terminal_run_after_stale_snapshot(monkeypatch, terminal_status):
    admin = _admin()
    run = _run(admin, status="running")
    _workflow(admin, run, status=WorkflowStatus.COMPLETED.value)
    finished_at = "2026-01-01T00:00:00+00:00"
    execute = FakeQuery.execute

    def cancel_before_workflow_lookup(query):
        if query._name == "workflows" and query._select is not None:
            run["status"] = terminal_status
            run["finished_at"] = finished_at
        return execute(query)

    monkeypatch.setattr(FakeQuery, "execute", cancel_before_workflow_lookup)
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch)))

    assert run["status"] == terminal_status
    assert run["finished_at"] == finished_at
    assert admin.tables["experiments"][0]["status"] == "approved"
    assert admin.rpc_calls == [("reconcile_execution_run", {
        "p_run_id": run["id"],
        "p_status": "succeeded",
        "p_error": None,
    })]


def test_reconciler_fails_stale_running_run_without_workflow(monkeypatch):
    admin = _admin()
    run = _run(admin, status="running")
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert run["status"] == "failed"
    assert run["finished_at"]
    assert "no workflow" in run["error"].lower()
    assert redis.jobs == []


def test_reconciler_fails_outbox_for_deleted_run(monkeypatch):
    admin = _admin()
    outbox = _outbox(admin, str(uuid4()))
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert outbox["status"] == "failed"
    assert "no longer exists" in outbox["last_error"].lower()
    assert redis.jobs == []


def test_reconciler_recovers_stale_run_with_nonterminal_workflow(monkeypatch):
    admin = _admin()
    run = _run(admin, status="running")
    workflow_id = str(uuid4())
    admin.table("workflows").insert({
        "id": workflow_id,
        "thread_id": workflow_id,
        "project_id": str(PROJECT_ID),
        "paper_id": str(uuid4()),
        "experiment_id": str(EXPERIMENT_ID),
        "run_id": run["id"],
        "status": WorkflowStatus.EXECUTING.value,
    }).execute()
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert run["status"] == "running"
    assert redis.enqueued == [
        ("recover_workflow_task", (workflow_id,), None)
    ]


def test_reconciler_leaves_fresh_dispatching_run_alone(monkeypatch):
    admin = _admin()
    run = _run(admin, status="dispatching", updated_at=FRESH)
    redis = FakeArq()
    asyncio.run(worker.reconcile_dispatch_task(_ctx(admin, monkeypatch, redis)))
    assert run["status"] == "dispatching"
    assert admin.tables["job_outbox"] == []
    assert redis.jobs == []


def test_worker_registers_30_second_reconciler_with_startup_run():
    assert worker.reconcile_dispatch_task in worker.WorkerSettings.functions
    cron_job = next(
        job for job in worker.WorkerSettings.cron_jobs
        if job.coroutine is worker.reconcile_dispatch_task
    )
    assert cron_job.second == {0, 30}
    assert cron_job.run_at_startup is True
    assert worker.WorkerSettings.max_tries == 1


@pytest.mark.asyncio
async def test_worker_startup_survives_redis_enqueue_failures(monkeypatch):
    admin = _admin()
    run = _run(admin)
    _outbox(admin, run["id"])
    workflow = _workflow(admin, run)
    comparison = admin.table("comparisons").insert({
        "experiment_id": str(EXPERIMENT_ID),
        "status": "running",
    }).execute().data[0]
    comparison_id = comparison["id"]
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )

    class FailingArq:
        def __init__(self):
            self.attempts = []

        async def enqueue_job(self, name, *args, _job_id=None):
            self.attempts.append((name, args, _job_id))
            raise ConnectionError("redis unavailable")

        async def set(self, key, value, ex=None):
            return True

    async def fake_checkpointer(url):
        return None, None

    redis = FailingArq()
    monkeypatch.setattr(worker, "ApiSettings", lambda: settings)
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "make_postgres_checkpointer", fake_checkpointer)

    await worker.WorkerSettings.on_startup({"redis": redis})

    assert redis.attempts == [
        ("run_experiment_task", (run["id"],), f"run-experiment:{run['id']}"),
        ("recover_workflow_task", (workflow["id"],), None),
        ("run_comparison_task", (comparison_id,), None),
    ]
    assert admin.tables["job_outbox"][0]["status"] == "pending"
    assert admin.tables["job_outbox"][0]["attempts"] == 1


@pytest.mark.asyncio
async def test_worker_startup_reenqueues_comparison_after_retained_job(monkeypatch):
    admin = _admin()
    comparison = admin.table("comparisons").insert({
        "experiment_id": str(EXPERIMENT_ID),
        "status": "running",
    }).execute().data[0]
    redis = FakeArq()
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )

    async def fake_checkpointer(url):
        return None, None

    monkeypatch.setattr(worker, "ApiSettings", lambda: settings)
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "make_postgres_checkpointer", fake_checkpointer)

    await worker.WorkerSettings.on_startup({"redis": redis})
    await worker.WorkerSettings.on_startup({"redis": redis})

    assert redis.enqueued == [
        ("run_comparison_task", (comparison["id"],), None),
        ("run_comparison_task", (comparison["id"],), None),
    ]


@pytest.mark.asyncio
async def test_worker_startup_dispatches_durable_outbox(monkeypatch):
    admin = _admin()
    run = _run(admin)
    _outbox(admin, run["id"])
    redis = FakeArq()
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )

    async def fake_checkpointer(url):
        return None, None

    monkeypatch.setattr(worker, "ApiSettings", lambda: settings)
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "make_postgres_checkpointer", fake_checkpointer)
    await worker.WorkerSettings.on_startup({"redis": redis})
    assert redis.enqueued == [
        ("run_experiment_task", (run["id"],), f"run-experiment:{run['id']}")
    ]
