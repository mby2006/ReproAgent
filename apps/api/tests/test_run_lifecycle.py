"""Offline regressions for execution cancellation, recovery, and terminal races."""

import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from reproagent_agent.codegen import generate_code
from reproagent_agent.planning import build_plan
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import WorkflowContext
from reproagent_schemas import (
    BudgetConfig,
    ErrorCategory,
    RunAccepted,
    RunResult,
    RunStatus,
    WorkflowStatus,
)
from test_runs import PROJECT_ID, USER_ID, FakeAdmin, make_card
from test_workflow_graph import PAPER_ID, _state, seed_workflow


def seed_execution(*, status="dispatching", attempt=0, runner_run_id=None):
    tables = seed_workflow()
    card = make_card()
    plan = build_plan(card)
    spec_id = str(uuid4())
    experiment_id = str(uuid4())
    plan_id = str(uuid4())
    run_id = str(uuid4())
    bundle = generate_code(card, plan)
    tables["reproduction_specs"].append({
        "id": spec_id,
        "paper_id": str(PAPER_ID),
        "version": 1,
        "spec_json": card.model_dump(mode="json"),
        "status": "confirmed",
    })
    tables["experiments"].append({
        "id": experiment_id,
        "project_id": str(PROJECT_ID),
        "paper_id": str(PAPER_ID),
        "spec_id": spec_id,
        "status": "running",
    })
    tables["experiment_plans"].append({
        "id": plan_id,
        "experiment_id": experiment_id,
        "version": 1,
        "config_json": plan.model_dump(mode="json"),
        "approved_at": datetime.now(UTC).isoformat(),
    })
    tables["execution_runs"].append({
        "id": run_id,
        "experiment_id": experiment_id,
        "plan_id": plan_id,
        "plan_version": 1,
        "code_version": 1,
        "code_hash": bundle.code_hash,
        "repair_attempt": attempt,
        "status": status,
        "runner_run_id": str(runner_run_id) if runner_run_id else None,
    })
    admin = FakeAdmin(tables, {str(PROJECT_ID): str(USER_ID)})
    state = _state(admin)
    state.update(
        card=card,
        plan=plan,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_version=1,
        spec_version_id=spec_id,
        spec_version=1,
        run_id=run_id,
        code_bundle=bundle,
        code_version=1,
        repair_attempt=attempt,
        budget_config=BudgetConfig(),
    )
    return admin, state, bundle


class StubRunner:
    def __init__(self, result=None, *, on_start=None, on_get=None):
        self.result = result or RunResult(run_id=uuid4(), status=RunStatus.SUCCEEDED)
        self.on_start = on_start
        self.on_get = on_get
        self.requests = []
        self.cancelled = []
        self.polls = 0

    async def start_run(self, request):
        self.requests.append(request)
        accepted = RunAccepted(run_id=self.result.run_id, status=RunStatus.QUEUED)
        if self.on_start is not None:
            self.on_start(accepted)
        return accepted

    async def get_run(self, run_id):
        self.polls += 1
        if self.on_get is not None:
            outcome = self.on_get()
            if asyncio.iscoroutine(outcome):
                await outcome
        return self.result.model_copy(update={"run_id": run_id})

    async def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        return self.result.model_copy(update={"run_id": run_id, "status": RunStatus.CANCELLED})

    async def get_artifact_bytes(self, run_id, name):
        return b"artifact"


def make_context(admin, runner):
    return WorkflowContext(
        admin=admin,
        agent=None,
        runner=runner,
        settings=ApiSettings(
            supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "succeeded"])
async def test_mark_attempt_running_cannot_reopen_protected_terminal(terminal):
    from reproagent_api.services.runs import mark_attempt_running

    admin, state, bundle = seed_execution(status=terminal)
    run = admin.tables["execution_runs"][0]

    claimed = mark_attempt_running(
        admin, run, attempt=0, code_version=1, code_hash=bundle.code_hash,
        plan_id=state["plan_id"], plan_version=1,
    )

    assert claimed is None
    assert run["status"] == terminal


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "attempt", "expected"), [
    ("failed", 1, "running"),
    ("timeout", 1, "running"),
    ("running", 0, "running"),
    ("failed", 0, "failed"),
])
async def test_attempt_transition_allows_only_deliberate_repair_or_same_recovery(
    status, attempt, expected
):
    from reproagent_api.services.runs import mark_attempt_running

    admin, state, bundle = seed_execution(status=status)
    run = admin.tables["execution_runs"][0]
    claimed = mark_attempt_running(
        admin, run, attempt=attempt, code_version=1, code_hash=bundle.code_hash,
        plan_id=state["plan_id"], plan_version=1,
    )
    assert (claimed is not None) is (expected == "running")
    assert run["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("code_version", "started_at", "expected"), [
    (0, None, "running"),
    (1, None, "queued"),
    (0, "2026-01-01T00:00:00+00:00", "queued"),
])
async def test_first_repair_attempt_requires_a_never_run_row(
    code_version, started_at, expected
):
    from reproagent_api.services.runs import mark_attempt_running

    admin, state, bundle = seed_execution(status="queued")
    run = admin.tables["execution_runs"][0]
    run["code_version"] = code_version
    run["started_at"] = started_at

    claimed = mark_attempt_running(
        admin, run, attempt=1, code_version=2, code_hash=bundle.code_hash,
        plan_id=state["plan_id"], plan_version=1,
    )

    assert (claimed is not None) is (expected == "running")
    assert run["status"] == expected


@pytest.mark.asyncio
async def test_cancellation_after_runner_acceptance_cancels_untracked_container():
    from reproagent_api.workflow.nodes import execute_code

    admin, state, _ = seed_execution()

    def cancel_after_acceptance(accepted):
        admin.table("execution_runs").update({"status": "cancelled"}).eq(
            "id", state["run_id"]
        ).execute()

    runner = StubRunner(on_start=cancel_after_acceptance)
    updates = await execute_code(state, make_context(admin, runner))

    assert updates["execution_result"].status is RunStatus.CANCELLED
    assert runner.cancelled == [runner.result.run_id]
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "cancelled"
    assert run.get("runner_run_id") is None


@pytest.mark.asyncio
async def test_cancellation_before_result_persistence_cancels_runner_and_seals_cancel():
    """Cancel after Runner accepted and returned, but before audit/artifact persistence."""
    from reproagent_api.workflow.nodes import execute_code

    admin, state, _ = seed_execution()
    workflow_id = admin.tables["workflows"][0]["id"]
    runner = StubRunner(RunResult(run_id=uuid4(), status=RunStatus.SUCCEEDED))

    async def cancel_before_persistence():
        # This is the persisted state written by the workflow cancellation endpoint
        # while execute_code is between Runner's terminal response and storage I/O.
        admin.table("workflows").update({
            "status": "cancelled", "error": "cancelled: workflow cancelled by user",
        }).eq("id", workflow_id).execute()
        admin.table("execution_runs").update({
            "status": "cancelled", "error": "cancelled: workflow cancelled by user",
        }).eq("id", state["run_id"]).execute()
        await runner.cancel_run(runner.result.run_id)

    runner.on_get = cancel_before_persistence
    updates = await execute_code(state, make_context(admin, runner))

    assert updates["execution_result"].status is RunStatus.CANCELLED
    assert runner.cancelled == [runner.result.run_id]
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "cancelled"
    assert run.get("metrics_json", {}) == {}
    assert admin.tables["workflows"][0]["status"] == "cancelled"


def pin_stored_code(admin, state, bundle):
    for item in bundle.files:
        admin.tables["generated_files"].append({
            "id": str(uuid4()),
            "experiment_id": state["experiment_id"],
            "version": 1,
            "path": item.path,
            "content": item.content,
            "sha256": item.sha256,
        })
    admin.tables["execution_run_files"].extend(
        {"run_id": state["run_id"], "generated_file_id": row["id"]}
        for row in admin.tables["generated_files"]
    )


@pytest.mark.asyncio
async def test_same_attempt_recovery_reattaches_without_post_or_code_insert():
    from reproagent_api.workflow.nodes import execute_code

    runner_run_id = uuid4()
    admin, state, bundle = seed_execution(status="running", runner_run_id=runner_run_id)
    pin_stored_code(admin, state, bundle)
    runner = StubRunner()

    updates = await execute_code(state, make_context(admin, runner))

    assert updates["execution_result"].status is RunStatus.SUCCEEDED
    assert runner.requests == []
    assert runner.polls == 1
    assert len(admin.tables["generated_files"]) == len(bundle.files)
    assert admin.tables["execution_runs"][0]["runner_run_id"] == str(runner_run_id)


@pytest.mark.asyncio
async def test_stored_code_without_runner_id_reuses_version_and_stable_identity():
    from reproagent_api.workflow.nodes import execute_code

    admin, state, bundle = seed_execution(status="running")
    pin_stored_code(admin, state, bundle)
    runner = StubRunner()

    await execute_code(state, make_context(admin, runner))

    assert len(runner.requests) == 1
    assert runner.requests[0].request_id == uuid5(
        NAMESPACE_URL, f"reproagent:execution:{state['run_id']}:attempt:0"
    )
    assert runner.requests[0].code_version == 1
    assert len(admin.tables["generated_files"]) == len(bundle.files)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "succeeded"])
async def test_late_result_cannot_overwrite_concurrent_protected_terminal(terminal):
    from reproagent_api.workflow.nodes import execute_code

    admin, state, _ = seed_execution()
    finished_at = "2026-01-01T00:00:00+00:00"

    def complete_before_poll():
        admin.table("execution_runs").update({
            "status": terminal, "finished_at": finished_at, "error": "real terminal result",
        }).eq("id", state["run_id"]).execute()

    runner = StubRunner(
        RunResult(run_id=uuid4(), status=RunStatus.FAILED, error="late failure"),
        on_get=complete_before_poll,
    )

    await execute_code(state, make_context(admin, runner))

    run = admin.tables["execution_runs"][0]
    assert run["status"] == terminal
    assert run["finished_at"] == finished_at
    assert run["error"] == "real terminal result"
    assert run.get("metrics_json", {}) == {}


@pytest.mark.asyncio
async def test_late_result_cannot_overwrite_a_newer_repair_attempt():
    from reproagent_api.workflow.nodes import execute_code

    admin, state, _ = seed_execution()

    def start_newer_attempt():
        run = admin.tables["execution_runs"][0]
        run.update({
            "repair_attempt": 1,
            "code_version": 2,
            "status": "running",
            "metrics_json": {"newer": True},
        })

    runner = StubRunner(
        RunResult(run_id=uuid4(), status=RunStatus.FAILED, error="late failure"),
        on_get=start_newer_attempt,
    )

    await execute_code(state, make_context(admin, runner))

    run = admin.tables["execution_runs"][0]
    assert run["repair_attempt"] == 1
    assert run["status"] == "running"
    assert run["metrics_json"] == {"newer": True}
    assert run.get("finished_at") is None


@pytest.mark.asyncio
async def test_stale_cancellation_cannot_mirror_over_a_newer_attempt():
    from reproagent_api.services.runs import finalize_execution_run

    admin, state, _ = seed_execution(status="cancelled", attempt=1)

    finalized = finalize_execution_run(
        admin, state["run_id"], RunStatus.CANCELLED, None, attempt=0
    )

    assert finalized is False
    assert admin.tables["execution_runs"][0]["repair_attempt"] == 1
    assert admin.tables["experiments"][-1]["status"] == "running"


@pytest.mark.asyncio
async def test_poll_exhaustion_cancels_runner_and_persists_timeout(monkeypatch):
    from reproagent_api.workflow import nodes
    from reproagent_api.workflow.nodes import execute_code

    monkeypatch.setattr(nodes, "POLL_INTERVAL_SECONDS", 0)
    admin, state, _ = seed_execution()
    runner = StubRunner(RunResult(run_id=uuid4(), status=RunStatus.RUNNING))

    updates = await execute_code(state, make_context(admin, runner))

    assert updates["execution_result"].status is RunStatus.TIMEOUT
    assert updates["execution_result"].error_category.value == "TIMEOUT"
    assert runner.cancelled == [runner.result.run_id]
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "timeout"
    assert run["finished_at"]
    assert admin.tables["experiments"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_invoke_cancellation_cancels_runner_and_finalizes_before_reraise():
    import asyncio
    from types import SimpleNamespace

    from reproagent_api.workflow.graph import _invoke

    admin, state, _ = seed_execution(status="running", runner_run_id=uuid4())
    runner = StubRunner()
    ctx = make_context(admin, runner)

    class CancelledGraph:
        async def ainvoke(self, payload, config):
            raise asyncio.CancelledError

        async def aget_state(self, config):
            return SimpleNamespace(values=state)

    with pytest.raises(asyncio.CancelledError):
        await _invoke(CancelledGraph(), state, {}, ctx, state)

    assert runner.cancelled == [UUID(admin.tables["execution_runs"][0]["runner_run_id"])]
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    assert admin.tables["workflows"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_persist_results_treats_cancelled_execution_as_cancelled_workflow():
    from reproagent_api.workflow.nodes import persist_results

    admin, state, _ = seed_execution(status="cancelled")
    state["execution_result"] = RunResult(
        run_id=UUID(state["run_id"]), status=RunStatus.CANCELLED
    )
    updates = await persist_results(state, make_context(admin, StubRunner()))
    assert updates["status"] == "cancelled"
    assert admin.tables["workflows"][0]["status"] == "cancelled"
    assert admin.tables["experiments"][0]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(("run_status", "code"), [
    (RunStatus.FAILED, "EXECUTION_FAILED"),
    (RunStatus.TIMEOUT, "TIMEOUT"),
])
async def test_persist_results_never_completes_failed_run(run_status, code):
    from reproagent_api.workflow.nodes import persist_results

    admin, state, _ = seed_execution(status=run_status.value)
    state["execution_result"] = RunResult(
        run_id=UUID(state["run_id"]), status=run_status, error="sandbox died"
    )
    updates = await persist_results(state, make_context(admin, StubRunner()))

    # A terminal Run failure is a terminal workflow failure: never completed.
    assert updates["status"] == WorkflowStatus.FAILED.value
    assert updates["error_code"] == code
    assert admin.tables["workflows"][0]["status"] == "failed"
    assert admin.tables["workflows"][0]["error"].startswith(f"{code}:")
    assert "sandbox died" in admin.tables["workflows"][0]["error"]
    # The Run keeps its own terminal status: timeout never degrades to failed.
    assert admin.tables["execution_runs"][0]["status"] == run_status.value


@pytest.mark.asyncio
async def test_persist_results_keeps_existing_pipeline_error_code():
    from reproagent_api.workflow.nodes import persist_results

    admin, state, _ = seed_execution(status=RunStatus.FAILED.value)
    state["error"] = "OUT_OF_MEMORY: repair budget exhausted"
    state["execution_result"] = RunResult(
        run_id=UUID(state["run_id"]), status=RunStatus.FAILED, error="CUDA out of memory"
    )
    updates = await persist_results(state, make_context(admin, StubRunner()))

    assert updates["status"] == WorkflowStatus.FAILED.value
    assert updates["error_code"] == "OUT_OF_MEMORY"
    assert admin.tables["workflows"][0]["error"] == "OUT_OF_MEMORY: repair budget exhausted"


@pytest.mark.parametrize(("run_status", "code"), [
    (RunStatus.FAILED, "EXECUTION_FAILED"),
    (RunStatus.TIMEOUT, "TIMEOUT"),
])
def test_cancellation_race_keeps_run_terminal_truth_and_workflow_cancelled(
    monkeypatch, run_status, code
):
    """Cancellation outranks the workflow write, the Run keeps its real outcome.

    The Runner reached a real terminal state while a cancellation was being
    persisted. The polling loop must not block past the cancellation, the workflow
    row stays CANCELLED, and the Run row keeps FAILED/TIMEOUT instead of being
    rewritten as cancelled or succeeded.
    """
    import asyncio
    from contextlib import contextmanager

    from reproagent_api import worker
    from reproagent_api.workflow.nodes import execute_code, persist_results

    @contextmanager
    def _always_acquired(settings, workflow_id):
        yield True

    admin, state, _ = seed_execution(status="running")
    runner_run_id = uuid4()
    admin.tables["execution_runs"][0]["runner_run_id"] = str(runner_run_id)
    workflow_id = admin.tables["workflows"][0]["id"]
    admin.tables["workflows"][0]["status"] = WorkflowStatus.CANCELLED.value
    admin.tables["workflows"][0]["run_id"] = state["run_id"]
    runner = StubRunner(RunResult(run_id=runner_run_id, status=run_status, error="boom"))
    ctx = make_context(admin, runner)

    updates = asyncio.run(execute_code(state, ctx))
    assert updates["execution_result"].status is run_status
    assert admin.tables["execution_runs"][0]["status"] == run_status.value
    assert runner.cancelled == [runner_run_id]

    terminal = asyncio.run(persist_results(state, ctx))
    assert terminal["status"] == WorkflowStatus.CANCELLED.value
    assert admin.tables["workflows"][0]["status"] == "cancelled"
    assert admin.tables["execution_runs"][0]["status"] == run_status.value

    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "_drive_lock", _always_acquired)
    worker_ctx = {"settings": ctx.settings, "redis": None}
    asyncio.run(worker.recover_workflow_task(worker_ctx, workflow_id))
    run = admin.tables["execution_runs"][0]
    assert run["status"] == run_status.value
    assert admin.tables["workflows"][0]["status"] == "cancelled"
    asyncio.run(worker._recover_existing_workflow(worker_ctx, admin, run, {
        "id": workflow_id, "status": WorkflowStatus.CANCELLED.value, "error": "cancelled",
    }))
    assert admin.tables["execution_runs"][0]["status"] == run_status.value


@pytest.mark.parametrize("workflow_status", ["completed", "failed", "cancelled"])
def test_worker_recovery_never_infers_success_from_workflow(monkeypatch, workflow_status):
    """Recovery reads the Run row's own terminal state; completed is not evidence."""
    import asyncio
    from contextlib import contextmanager

    from reproagent_api import worker

    @contextmanager
    def _always_acquired(settings, workflow_id):
        yield True

    admin, state, _ = seed_execution(status="running", attempt=1)
    admin.tables["workflows"][0]["status"] = workflow_status
    admin.tables["workflows"][0]["run_id"] = state["run_id"]
    admin.tables["workflows"][0]["error"] = "EXECUTION_FAILED: sandbox died"
    monkeypatch.setattr(worker, "_drive_lock", _always_acquired)
    ctx = {"settings": ApiSettings(supabase_service_role_key="s", _env_file=None), "redis": None}
    run = admin.tables["execution_runs"][0]

    asyncio.run(worker._recover_existing_workflow(
        ctx, admin, run, dict(admin.tables["workflows"][0])
    ))

    run = admin.tables["execution_runs"][0]
    assert run["status"] != RunStatus.SUCCEEDED.value
    assert run["status"] == (
        RunStatus.CANCELLED.value if workflow_status == "cancelled" else RunStatus.FAILED.value
    )
    assert admin.tables["workflows"][0]["status"] == workflow_status


@pytest.mark.asyncio
async def test_worker_recovery_leaves_recorded_terminal_run_untouched():
    from reproagent_api import worker

    admin, state, _ = seed_execution(status=RunStatus.SUCCEEDED.value, attempt=1)
    admin.tables["workflows"][0]["status"] = WorkflowStatus.COMPLETED.value
    finished_at = "2026-01-01T00:00:00+00:00"
    admin.tables["execution_runs"][0]["finished_at"] = finished_at
    ctx = {"settings": ApiSettings(supabase_service_role_key="s", _env_file=None), "redis": None}

    await worker._recover_existing_workflow(
        ctx, admin, admin.tables["execution_runs"][0], dict(admin.tables["workflows"][0])
    )

    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["finished_at"] == finished_at


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["request", "poll", "persist"])
async def test_runner_exception_finalizes_run_without_leaking_terminal_race(failure, monkeypatch):
    from reproagent_api.workflow import nodes
    from reproagent_api.workflow.nodes import execute_code

    admin, state, _ = seed_execution()
    runner = StubRunner()

    async def fail_start(request):
        raise ConnectionError("runner unavailable")

    async def fail_get(run_id):
        raise ConnectionError("runner unavailable")

    async def fail_persist(*args):
        raise OSError("storage unavailable")

    if failure == "request":
        runner.start_run = fail_start
    elif failure == "poll":
        runner.get_run = fail_get
    else:
        monkeypatch.setattr(nodes, "async_persist_result", fail_persist)
    updates = await execute_code(state, make_context(admin, runner))

    expected = RunStatus.SUCCEEDED if failure == "persist" else RunStatus.FAILED
    assert updates["execution_result"].status is expected
    run = admin.tables["execution_runs"][0]
    assert run["status"] == expected.value
    assert run["finished_at"]
    if failure == "poll":
        assert runner.cancelled == [runner.result.run_id]


# --- Task 7: append-only attempt history ------------------------------------


def _seed_attempt_run(*, attempt=0, status="dispatching"):
    """Return (admin, state, bundle, run_id, plan_id) with attempt history tables seeded."""
    admin, state, bundle = seed_execution(status=status, attempt=attempt)
    admin.tables.setdefault("execution_attempts", [])
    admin.tables.setdefault("execution_attempt_files", [])
    admin.tables.setdefault("generated_files", [])
    return admin, state, bundle, state["run_id"], state["plan_id"]


def _insert_generated_files(admin, experiment_id, prefix, count=3, version=1):
    ids = []
    for i in range(count):
        fid = str(uuid4())
        admin.tables["generated_files"].append({
            "id": fid,
            "experiment_id": experiment_id,
            "version": version,
            "path": f"{prefix}/file_{i}.py",
            "sha256": f"sha-{prefix}-{i}",
            "content": "",
            "change_reason": "seed",
            "related_error": None,
        })
        ids.append(fid)
    return ids


def test_pin_attempt_files_second_attempt_does_not_erase_first():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    first_ids = _insert_generated_files(admin, state["experiment_id"], "v1", version=1)
    second_ids = _insert_generated_files(admin, state["experiment_id"], "v2", version=2)

    pin_attempt_files(admin, run_id, 1, first_ids)
    pin_attempt_files(admin, run_id, 2, second_ids)

    assert len(admin.rpc_builders) == 2
    assert all(builder.executed for builder in admin.rpc_builders)
    attempts = sorted(
        row["attempt"] for row in admin.tables["execution_attempts"]
        if row["run_id"] == run_id
    )
    assert attempts == [1, 2]

    first_pinned = {
        row["generated_file_id"] for row in admin.tables["execution_attempt_files"]
        if row["run_id"] == run_id and row["attempt"] == 1
    }
    second_pinned = {
        row["generated_file_id"] for row in admin.tables["execution_attempt_files"]
        if row["run_id"] == run_id and row["attempt"] == 2
    }
    assert first_pinned == set(first_ids)
    assert second_pinned == set(second_ids)


def test_list_attempts_returns_attempts_in_order_with_files():
    from reproagent_api.services.runs import list_attempts, pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    first_ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=2)
    second_ids = _insert_generated_files(admin, state["experiment_id"], "v2", count=2)

    pin_attempt_files(admin, run_id, 1, first_ids)
    pin_attempt_files(admin, run_id, 2, second_ids)

    history = list_attempts(admin, run_id)
    assert [a["attempt"] for a in history] == [1, 2]
    assert {f["generated_file_id"] for f in history[0]["files"]} == set(first_ids)
    assert {f["generated_file_id"] for f in history[1]["files"]} == set(second_ids)


def test_pin_attempt_files_is_idempotent_for_same_attempt_and_files():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=3)

    pin_attempt_files(admin, run_id, 1, ids)
    pin_attempt_files(admin, run_id, 1, ids)

    attempt_rows = [r for r in admin.tables["execution_attempts"]
                    if r["run_id"] == run_id and r["attempt"] == 1]
    assert len(attempt_rows) == 1
    file_rows = [r for r in admin.tables["execution_attempt_files"]
                 if r["run_id"] == run_id and r["attempt"] == 1]
    assert len(file_rows) == 3  # not duplicated


def test_pin_attempt_files_rejects_nonempty_after_an_explicit_empty_pin():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    replacement = _insert_generated_files(admin, state["experiment_id"], "v1", count=1)
    pin_attempt_files(admin, run_id, 1, [])

    with pytest.raises(ValueError, match="already pinned a different file set"):
        pin_attempt_files(admin, run_id, 1, replacement)

    assert [
        row for row in admin.tables["execution_attempt_files"]
        if row["run_id"] == run_id and row["attempt"] == 1
    ] == []


def test_pin_attempt_files_rejects_a_different_file_set_without_union():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    original = _insert_generated_files(admin, state["experiment_id"], "v1", count=2)
    replacement = _insert_generated_files(admin, state["experiment_id"], "v2", count=1)
    pin_attempt_files(admin, run_id, 1, original)

    with pytest.raises(ValueError, match="already pinned a different file set"):
        pin_attempt_files(admin, run_id, 1, replacement)

    pinned = {
        row["generated_file_id"] for row in admin.tables["execution_attempt_files"]
        if row["run_id"] == run_id and row["attempt"] == 1
    }
    assert pinned == set(original)


def test_pin_attempt_files_same_set_in_different_order_is_a_noop():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=3)
    pin_attempt_files(admin, run_id, 1, ids)
    before = [dict(row) for row in admin.tables["execution_attempt_files"]]

    pin_attempt_files(admin, run_id, 1, list(reversed(ids)))

    assert admin.tables["execution_attempt_files"] == before


def test_pin_attempt_files_rejects_duplicate_input_ids():
    from reproagent_api.services.runs import pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=1)

    with pytest.raises(ValueError, match="must not contain duplicates"):
        pin_attempt_files(admin, run_id, 1, ids + ids)

    assert admin.tables["execution_attempts"] == []
    assert admin.tables["execution_attempt_files"] == []


def test_pin_run_files_replaces_projection_with_one_rpc():
    from reproagent_api.services.runs import pin_run_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    first_ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=1)
    second_ids = _insert_generated_files(admin, state["experiment_id"], "v2", count=2)
    pin_run_files(admin, run_id, first_ids, attempt=1)

    pin_run_files(admin, run_id, second_ids, attempt=2)

    assert len(admin.rpc_builders) == 4
    assert all(builder.executed for builder in admin.rpc_builders)
    assert admin.rpc_calls[-1] == (
        "replace_run_projection",
        {"p_run_id": run_id, "p_attempt": 2, "p_file_ids": second_ids},
    )
    assert {
        row["generated_file_id"] for row in admin.tables["execution_run_files"]
        if row["run_id"] == run_id
    } == set(second_ids)


def test_projection_rpc_failure_does_not_partially_replace_projection():
    from reproagent_api.services.runs import pin_run_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0)
    original = _insert_generated_files(admin, state["experiment_id"], "v1", count=1)
    replacement = _insert_generated_files(admin, state["experiment_id"], "v2", count=1)
    pin_run_files(admin, run_id, original, attempt=1)
    # A missing file makes the SQL insert fail after its delete; the transaction
    # must roll both operations back rather than leaving an empty projection.
    invalid = [str(uuid4())]

    with pytest.raises(ValueError, match="generated file does not exist"):
        pin_run_files(admin, run_id, invalid, attempt=2)

    assert {
        row["generated_file_id"] for row in admin.tables["execution_run_files"]
        if row["run_id"] == run_id
    } == set(original)
    assert replacement[0] not in {
        row["generated_file_id"] for row in admin.tables["execution_run_files"]
    }


def test_persist_runner_run_id_updates_only_matching_attempt_identity():
    from reproagent_api.services.runs import (
        execution_request_id,
        persist_runner_run_id,
        pin_attempt_files,
    )

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=1, status="failed")
    first_files = _insert_generated_files(admin, state["experiment_id"], "v1", version=1)
    second_files = _insert_generated_files(admin, state["experiment_id"], "v2", version=2)
    pin_attempt_files(admin, run_id, 1, first_files)
    pin_attempt_files(admin, run_id, 2, second_files)
    first_runner = uuid4()
    first = admin.tables["execution_attempts"][0]
    first.update({
        "runner_run_id": str(first_runner),
        "runner_request_id": str(execution_request_id(UUID(run_id), 0)),
    })

    second_runner = uuid4()
    assert persist_runner_run_id(admin, run_id, second_runner, 1) is True

    history = {row["attempt"]: row for row in admin.tables["execution_attempts"]}
    assert history[1]["runner_run_id"] == str(first_runner)
    assert history[1]["runner_request_id"] == str(execution_request_id(UUID(run_id), 0))
    assert history[2]["runner_run_id"] == str(second_runner)
    assert history[2]["runner_request_id"] == str(execution_request_id(UUID(run_id), 1))


def test_pin_attempt_files_populates_zero_based_runner_request_identity():
    from reproagent_api.services.runs import execution_request_id, pin_attempt_files

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=1)
    files = _insert_generated_files(admin, state["experiment_id"], "v2", version=2)

    pin_attempt_files(admin, run_id, 2, files)

    attempt = admin.tables["execution_attempts"][0]
    assert attempt["runner_request_id"] == str(execution_request_id(UUID(run_id), 1))


def test_pin_run_files_updates_projection_but_keeps_history():
    """pin_run_files remains the final-attempt projection writer; history appends."""
    from reproagent_api.services.runs import pin_run_files

    admin, state, _, run_id, plan_id = _seed_attempt_run(attempt=0)
    first_ids = _insert_generated_files(admin, state["experiment_id"], "v1", count=2)
    second_ids = _insert_generated_files(admin, state["experiment_id"], "v2", count=2)
    # Populate run row with plan_id/code_version so pin_run_files can read them.
    admin.tables["execution_runs"][0]["plan_id"] = plan_id
    admin.tables["execution_runs"][0]["code_version"] = 1
    admin.tables["execution_runs"][0]["plan_version"] = 1

    # attempt=1 (1-based) pin: writes projection and history.
    pin_run_files(admin, run_id, first_ids, attempt=1)
    # attempt=2: projection replaced with second batch, history preserved.
    admin.tables["execution_runs"][0]["code_version"] = 2
    pin_run_files(admin, run_id, second_ids, attempt=2)

    projection = {
        r["generated_file_id"] for r in admin.tables["execution_run_files"]
        if r["run_id"] == run_id
    }
    assert projection == set(second_ids)

    first_pinned = {
        r["generated_file_id"] for r in admin.tables["execution_attempt_files"]
        if r["run_id"] == run_id and r["attempt"] == 1
    }
    second_pinned = {
        r["generated_file_id"] for r in admin.tables["execution_attempt_files"]
        if r["run_id"] == run_id and r["attempt"] == 2
    }
    assert first_pinned == set(first_ids)
    assert second_pinned == set(second_ids)


def test_mark_attempt_running_writes_history_row():
    from reproagent_api.services.runs import mark_attempt_running

    # A failed Run with repair_attempt=0 permits a deliberate repair to attempt=1
    # (repair_attempt) / history attempt=2.
    admin, state, bundle, run_id, plan_id = _seed_attempt_run(attempt=0, status="failed")
    run = admin.tables["execution_runs"][0]

    claimed = mark_attempt_running(
        admin, run, attempt=1, code_version=2, code_hash=bundle.code_hash,
        plan_id=plan_id, plan_version=1,
    )

    assert claimed is not None
    rows = [r for r in admin.tables["execution_attempts"]
            if r["run_id"] == run_id and r["attempt"] == 2]
    assert len(rows) == 1  # 1-based mapping: repair_attempt 1 -> attempt 2
    assert rows[0]["plan_id"] == plan_id
    assert rows[0]["code_version"] == 2
    assert rows[0]["status"] == "running"


def test_async_persist_result_writes_attempt_on_artifacts():
    """async_persist_result records 1-based attempt on artifact rows and storage path."""
    import asyncio

    from reproagent_api.services.runs import async_persist_result

    admin, state, _, run_id, plan_id = _seed_attempt_run(attempt=0, status="running")
    _seed_persistable_attempt(admin, state, run_id)
    runner_run_id = uuid4()
    admin.tables["execution_runs"][0]["runner_run_id"] = str(runner_run_id)
    result = RunResult(
        run_id=runner_run_id,
        status=RunStatus.SUCCEEDED,
        exit_code=0,
        stdout="ok",
        stderr="",
        execution_time=0.1,
        metrics={"metric_value": 0.9},
        artifacts=["metrics.json"],
    )

    class _Runner:
        async def get_artifact_bytes(self, run_id, name):
            return b"payload"

    # repair_attempt=0 -> attempt=1 in the DB and 1-based storage path.
    asyncio.run(async_persist_result(admin, run_id, runner_run_id, result, _Runner(), 0))

    arts = [r for r in admin.tables["execution_artifacts"] if r["run_id"] == run_id]
    assert len(arts) == 1
    assert arts[0]["attempt"] == 1
    assert arts[0]["storage_path"].startswith(f"{run_id}/attempt-1/")


def test_async_persist_result_never_erases_prior_attempt_artifacts():
    """A repair persists its own artifacts; the earlier attempt's rows survive."""
    import asyncio

    from reproagent_api.services.runs import async_persist_result

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=1, status="running")
    _seed_persistable_attempt(admin, state, run_id, attempt=0)
    _seed_persistable_attempt(admin, state, run_id, attempt=1)
    # Pre-existing artifact from the first attempt (attempt=1 in history terms).
    admin.tables["execution_artifacts"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": 1,
        "artifact_type": "metrics.json",
        "storage_path": f"{run_id}/attempt-1/artifacts/metrics.json",
        "metadata_json": {"attempt": 1},
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    runner_run_id = uuid4()
    admin.tables["execution_runs"][0]["runner_run_id"] = str(runner_run_id)
    result = RunResult(
        run_id=runner_run_id,
        status=RunStatus.FAILED,
        exit_code=1,
        stdout="",
        stderr="bad",
        execution_time=0.2,
        metrics={},
        artifacts=["metrics.json"],
        error="boom",
    )

    class _Runner:
        async def get_artifact_bytes(self, run_id, name):
            return b"payload2"

    # repair_attempt=1 -> history attempt=2 storage path.
    asyncio.run(async_persist_result(admin, run_id, runner_run_id, result, _Runner(), 1))

    arts = sorted(
        (r for r in admin.tables["execution_artifacts"] if r["run_id"] == run_id),
        key=lambda r: r["attempt"],
    )
    assert [a["attempt"] for a in arts] == [1, 2]
    assert arts[0]["storage_path"] == f"{run_id}/attempt-1/artifacts/metrics.json"
    assert arts[1]["storage_path"] == f"{run_id}/attempt-2/artifacts/metrics.json"


def _seed_persistable_attempt(admin, state, run_id, attempt=0):
    from reproagent_api.services.runs import ensure_attempt_row

    return ensure_attempt_row(admin, admin.tables["execution_runs"][0], attempt + 1)


@pytest.mark.asyncio
async def test_cancel_after_result_claim_before_artifact_replace():
    """Cancellation after the persistence claim cannot delete or replace evidence."""
    from reproagent_api.services.runs import async_persist_result

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0, status="running")
    _seed_persistable_attempt(admin, state, run_id)
    run = admin.tables["execution_runs"][0]
    run.update({
        "metrics_json": {"protected": True},
        "audit_json": {"seed": 42},
        "stdout_path": "protected/stdout.txt",
        "stderr_path": "protected/stderr.txt",
        "error": "cancelled: workflow cancelled by user",
    })
    protected = {
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": 1,
        "artifact_type": "protected.json",
        "storage_path": f"{run_id}/attempt-1/artifacts/protected.json",
        "metadata_json": {"attempt": 1},
    }
    admin.tables["execution_artifacts"].append(protected)

    class CancellingRunner:
        async def get_artifact_bytes(self, runner_run_id, name):
            # The result has been claimed; this is the pre-replacement race window.
            assert run["status"] == "running"
            run.update({"status": "cancelled", "error": "cancelled: workflow cancelled by user"})
            return b"late artifact"

    result = RunResult(
        run_id=uuid4(), status=RunStatus.SUCCEEDED, metrics={"late": True},
        artifacts=["late.json"], audit={"seed": 99},
    )
    await async_persist_result(admin, run_id, result.run_id, result, CancellingRunner(), 0)

    assert admin.tables["execution_artifacts"] == [protected]
    assert run["status"] == "cancelled"
    assert run["error"] == "cancelled: workflow cancelled by user"
    assert run["metrics_json"] == {"protected": True}
    assert run["audit_json"] == {"seed": 42}
    assert run["stdout_path"] == "protected/stdout.txt"
    assert run["stderr_path"] == "protected/stderr.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "timeout", "succeeded"])
async def test_terminal_audit_survives_result_claim_before_artifact_replace(terminal):
    """Every persisted terminal status seals audit/error/paths and current artifacts."""
    from reproagent_api.services.runs import async_persist_result

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0, status="running")
    _seed_persistable_attempt(admin, state, run_id)
    run = admin.tables["execution_runs"][0]
    run.update({
        "metrics_json": {"protected": True},
        "audit_json": {"seed": 42},
        "stdout_path": "protected/stdout.txt",
        "stderr_path": "protected/stderr.txt",
        "error_category": "TIMEOUT",
        "error": "persisted terminal truth",
    })
    protected = {
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": 1,
        "artifact_type": "protected.json",
        "storage_path": f"{run_id}/attempt-1/artifacts/protected.json",
        "metadata_json": {"attempt": 1},
    }
    admin.tables["execution_artifacts"].append(protected)

    class TerminalRunner:
        async def get_artifact_bytes(self, runner_run_id, name):
            assert run["status"] == "running"
            run.update({"status": terminal, "error": "persisted terminal truth"})
            return b"late artifact"

    result = RunResult(
        run_id=uuid4(), status=RunStatus.FAILED, error="late error",
        error_category=ErrorCategory.UNKNOWN_ERROR,
        metrics={"late": True}, artifacts=["late.json"], audit={"seed": 99},
    )
    await async_persist_result(admin, run_id, result.run_id, result, TerminalRunner(), 0)

    assert admin.tables["execution_artifacts"] == [protected]
    assert run["status"] == terminal
    assert run["error"] == "persisted terminal truth"
    assert run["error_category"] == "TIMEOUT"
    assert run["metrics_json"] == {"protected": True}
    assert run["audit_json"] == {"seed": 42}
    assert run["stdout_path"] == "protected/stdout.txt"
    assert run["stderr_path"] == "protected/stderr.txt"


def test_terminal_attempt_history_cannot_be_reopened_by_stale_writer():
    """A stale finalize_execution_run never overwrites a sealed attempt row.

    The projection RPC reflects whatever the run row's own state machine allows;
    the attempt-history row is separately guarded so a succeeded attempt is
    never downgraded to failed by a late writer.
    """
    from reproagent_api.services.runs import finalize_execution_run

    admin, state, _, run_id, _ = _seed_attempt_run(attempt=0, status="running")
    # Pre-seal the attempt history row as succeeded.
    admin.tables["execution_attempts"].append({
        "id": str(uuid4()),
        "run_id": run_id,
        "attempt": 1,
        "plan_id": state["plan_id"],
        "plan_version": 1,
        "code_version": 1,
        "status": "succeeded",
        "finished_at": "2026-01-01T00:00:02+00:00",
    })

    finalize_execution_run(admin, run_id, RunStatus.FAILED, "late failure", attempt=0)

    row = admin.tables["execution_attempts"][0]
    # The attempt row stays succeeded: the guarded update refuses to regress it.
    assert row["status"] == "succeeded"
    assert row["finished_at"] == "2026-01-01T00:00:02+00:00"
