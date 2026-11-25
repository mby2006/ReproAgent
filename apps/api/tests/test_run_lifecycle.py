"""Offline regressions for execution cancellation, recovery, and terminal races."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from reproagent_agent.codegen import generate_code
from reproagent_agent.planning import build_plan
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import WorkflowContext
from reproagent_schemas import BudgetConfig, RunAccepted, RunResult, RunStatus
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
            self.on_get()
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
