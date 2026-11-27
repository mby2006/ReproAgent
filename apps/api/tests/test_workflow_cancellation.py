"""Cancellation is a first-class workflow outcome reachable from every phase.

These tests drive the real 15-node graph with the stub transports and assert the
three properties the terminal-truth contract needs: cancellation is persisted
before the API returns, every non-terminal node boundary can divert to the
terminal ``cancel_workflow`` node, and neither a worker restart nor a late Runner
result can turn a cancelled workflow into a completed one or fabricate a
succeeded result.
"""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from reproagent_api import worker
from reproagent_api.clients.agent import AgentClient
from reproagent_api.clients.runner import RunnerClient
from reproagent_api.config import ApiSettings
from reproagent_api.services.runs import execution_request_id, finalize_execution_run
from reproagent_api.workflow import (
    WorkflowContext,
    build_workflow_graph,
    continue_workflow,
    make_memory_checkpointer,
    resume_workflow,
    start_workflow,
)
from reproagent_api.workflow.graph import NODE_ORDER
from reproagent_api.workflow.nodes import (
    cancel_requested,
    cancel_workflow,
    execute_code,
    persist_results,
)
from reproagent_schemas import RunResult, RunStatus, WorkflowStatus
from test_run_lifecycle import seed_execution
from test_runs import (
    EXPERIMENT_ID,
    FakeAdmin,
    FakeArq,
    PROJECT_ID,
    USER_ID,
    make_result,
    runner_transport,
)
from test_workflow_graph import (
    PAPER_ID,
    THREAD_ID,
    _state,
    _workflow,
    seed_workflow,
    workflow_agent_transport,
)
from test_workflows import AUTH, WORKFLOW_ID, _seed

OWNERS = {str(PROJECT_ID): str(USER_ID)}


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    """Never let a polling guard regress into a real 700-second wait."""
    from reproagent_api.workflow import nodes

    monkeypatch.setattr(nodes, "POLL_INTERVAL_SECONDS", 0)


def cancel_now(admin, *, finalize_run=True):
    """Persist what ``POST /v1/workflows/{id}/cancel`` persists before returning."""
    admin.table("workflows").update({"status": WorkflowStatus.CANCELLED.value}).eq(
        "id", admin.tables["workflows"][0]["id"]
    ).execute()
    rows = admin.tables["execution_runs"] if finalize_run else []
    for row in rows:
        if row["status"] in ("succeeded", "failed", "timeout", "cancelled"):
            continue
        finalize_execution_run(
            admin,
            row["id"],
            RunStatus.CANCELLED,
            "cancelled: workflow cancelled by user",
            attempt=row.get("repair_attempt", 0),
            require_latest=True,
        )


def make_ctx(admin, results, *, agent_path=None, runner_path=None, runner_records=None):
    """Context whose transport flips the persisted cancellation flag mid-flight."""
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    admin.storage.files["u/p.pdf"] = b"%PDF-1.4 fake"
    base_agent = workflow_agent_transport()

    def agent_handler(request: httpx.Request) -> httpx.Response:
        response = base_agent.handle_request(request)
        if agent_path and request.url.path == agent_path:
            cancel_now(admin, finalize_run=False)
        return response

    base_runner = runner_transport(results)

    def runner_handler(request: httpx.Request) -> httpx.Response:
        if runner_records is not None:
            runner_records.append(f"{request.method} {request.url.path}")
        if runner_path and request.url.path.startswith(runner_path):
            cancel_now(admin, finalize_run=False)
        return base_runner.handle_request(request)

    return WorkflowContext(
        admin=admin,
        agent=AgentClient(settings, transport=httpx.MockTransport(agent_handler)),
        runner=RunnerClient(settings, transport=httpx.MockTransport(runner_handler)),
        settings=settings,
    )


# ---------------------------------------------------------------- layer: nodes


def test_cancel_requested_reads_the_persisted_flag():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    ctx = make_ctx(admin, [])
    state = _state(admin)
    try:
        assert cancel_requested(state, ctx) is False
        admin.table("workflows").update({"status": "executing"}).eq(
            "id", admin.tables["workflows"][0]["id"]
        ).execute()
        assert cancel_requested(state, ctx) is False
        cancel_now(admin, finalize_run=False)
        assert cancel_requested(state, ctx) is True
        # a workflow without a row (legacy runs entry) is never "cancelled"
        assert cancel_requested({"workflow_id": str(uuid4())}, ctx) is False
    finally:
        asyncio.run(ctx.agent.aclose())
        asyncio.run(ctx.runner.aclose())


def test_cancel_workflow_node_keeps_results_and_writes_no_report():
    admin, state, _ = seed_execution(status="running")
    run_id = state["run_id"]
    admin.tables["reproduction_reports"].append({
        "run_id": run_id, "report_json": {"final_status": "PARTIAL"}, "markdown": "kept"
    })
    runner_run_id = uuid4()
    admin.tables["execution_runs"][0]["runner_run_id"] = str(runner_run_id)
    records: list[str] = []
    ctx = make_ctx(admin, [make_result(RunStatus.RUNNING, None)], runner_records=records)
    state.update(execution_result=make_result(RunStatus.FAILED, 1))
    try:
        updates = asyncio.run(cancel_workflow(state, ctx))
    finally:
        asyncio.run(ctx.agent.aclose())
        asyncio.run(ctx.runner.aclose())

    assert updates["status"] == WorkflowStatus.CANCELLED.value
    assert updates["current_node"] == "cancel_workflow"
    assert _workflow(admin)["status"] == "cancelled"
    # the Runner owns the container: it is cancelled with the accepted id
    assert f"POST /runs/{runner_run_id}/cancel" in records
    # the run row records the cancellation, the workflow keeps its prior result
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    assert admin.tables["reproduction_reports"][0]["markdown"] == "kept"
    assert len(admin.tables["reproduction_reports"]) == 1


def test_cancel_workflow_finalizes_the_latest_attempt_only():
    from reproagent_api.services.runs import load_run_row

    admin, state, _ = seed_execution(status="running", attempt=2)
    ctx = make_ctx(admin, [])
    try:
        asyncio.run(cancel_workflow(state, ctx))
    finally:
        asyncio.run(ctx.agent.aclose())
        asyncio.run(ctx.runner.aclose())
    run = load_run_row(admin, state["run_id"])
    assert run["status"] == "cancelled"
    assert run["repair_attempt"] == 2


def test_cancel_workflow_cancels_the_deterministic_identity_before_acceptance():
    from reproagent_api.services.runs import execution_request_id, load_run_row

    admin, state, _ = seed_execution(status="dispatching")
    records: list[str] = []
    ctx = make_ctx(admin, [], runner_records=records)
    try:
        asyncio.run(cancel_workflow(state, ctx))
    finally:
        asyncio.run(ctx.agent.aclose())
        asyncio.run(ctx.runner.aclose())
    expected = execution_request_id(UUID(state["run_id"]), 0)
    assert f"POST /runs/{expected}/cancel" in records
    assert load_run_row(admin, state["run_id"])["status"] == "cancelled"


# ------------------------------------------------------------- layer: graph


def test_every_non_terminal_node_can_divert_to_cancellation():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    ctx = make_ctx(admin, [])
    try:
        graph = build_workflow_graph(ctx, make_memory_checkpointer())
    finally:
        asyncio.run(ctx.agent.aclose())
        asyncio.run(ctx.runner.aclose())
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    driven = {name for name, _ in NODE_ORDER} - {"persist_results", "cancel_workflow"}
    # every node that can still do work has a cancellation exit
    missing = {name for name in driven if (name, "cancel_workflow") not in edges}
    assert missing == set()
    assert ("cancel_workflow", "__end__") in edges
    assert ("persist_results", "__end__") in edges


# ------------------------------------------------------- layer: five timepoints


@pytest.mark.asyncio
async def test_cancel_at_spec_review_survives_a_worker_restart():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    ctx = make_ctx(admin, [make_result(RunStatus.SUCCEEDED, 0)])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value
        cancel_now(admin, finalize_run=False)
        # the gate can no longer be resumed, and recovery re-drives it as cancelled
        assert worker.resume_allowed(_workflow(admin)) is False
        await continue_workflow(ctx, saver, THREAD_ID, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert admin.tables["experiments"] == []
        assert admin.tables["execution_runs"] == []
        assert admin.tables["reproduction_reports"] == []
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


@pytest.mark.asyncio
async def test_cancel_at_plan_approval_survives_a_worker_restart():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    ctx = make_ctx(admin, [make_result(RunStatus.SUCCEEDED, 0)])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.AWAITING_PLAN_APPROVAL.value
        cancel_now(admin, finalize_run=False)
        await continue_workflow(ctx, saver, THREAD_ID, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert admin.tables["experiment_plans"][-1]["approved_at"] is None
        assert admin.tables["execution_runs"] == []
        assert admin.tables["reproduction_reports"] == []
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


@pytest.mark.asyncio
async def test_cancel_after_codegen_never_dispatches_to_the_runner():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    records: list[str] = []
    ctx = make_ctx(
        admin,
        [make_result(RunStatus.SUCCEEDED, 0)],
        agent_path="/v1/codegen",
        runner_records=records,
    )
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
        admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "plan"}, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert [path for path in records if path.startswith("POST /runs")] == []
        assert admin.tables["execution_runs"][0]["status"] == "cancelled"
        assert admin.tables["reproduction_reports"] == []
        traced = {trace["node_name"] for trace in admin.tables["agent_traces"]}
        assert "execute_code" not in traced and "generate_report" not in traced
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


@pytest.mark.asyncio
async def test_cancel_after_runner_acceptance_stops_polling_and_stays_cancelled():
    admin = FakeAdmin(seed_workflow(), OWNERS)
    records: list[str] = []
    # the scripted Runner result is still running: nothing may be recorded as success
    ctx = make_ctx(
        admin,
        [make_result(RunStatus.RUNNING, None)],
        runner_path="/runs/",
        runner_records=records,
    )
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
        admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "plan"}, _state(admin))
        run = admin.tables["execution_runs"][0]
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert run["status"] == "cancelled"
        assert run["runner_run_id"]
        assert f"POST /runs/{run['runner_run_id']}/cancel" in records
        assert admin.tables["reproduction_reports"] == []
        # restart: recovery leaves both layers cancelled
        assert worker.resume_allowed(_workflow(admin)) is False
        await continue_workflow(ctx, saver, THREAD_ID, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


@pytest.mark.asyncio
async def test_cancel_just_before_the_result_is_written(monkeypatch):
    from reproagent_api.workflow import nodes

    admin = FakeAdmin(seed_workflow(), OWNERS)
    ctx = make_ctx(admin, [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.99})])
    saver = make_memory_checkpointer()
    succeeded = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.99})
    original = nodes.async_persist_result
    raced = {"hit": False}

    def race_on_write(admin_, run_id, runner_run_id, result, runner, attempt):
        # cancellation lands after the Runner finished but before persistence
        if not raced["hit"]:
            raced["hit"] = True
            cancel_now(admin_)
        return original(admin_, run_id, runner_run_id, result, runner, attempt)

    monkeypatch.setattr(nodes, "async_persist_result", race_on_write)
    try:
        await start_workflow(ctx, saver, _state(admin))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
        admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "plan"}, _state(admin))
        assert raced["hit"] is True
        assert _workflow(admin)["status"] == WorkflowStatus.CANCELLED.value
        assert admin.tables["execution_runs"][0]["status"] == "cancelled"
        assert admin.tables["experiments"][-1]["status"] != "succeeded"
        assert admin.tables["reproduction_reports"] == []
        assert succeeded.status is RunStatus.SUCCEEDED  # the fake never mutated
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


# ------------------------------------------------------ layer: driver internals


@pytest.mark.asyncio
async def test_poll_guard_cancels_runner_instead_of_waiting_out_the_loop():
    admin, state, _ = seed_execution(status="running")
    records: list[str] = []
    runner_run_id = uuid4()
    admin.tables["execution_runs"][0]["runner_run_id"] = str(runner_run_id)
    ctx = make_ctx(
        admin,
        [make_result(RunStatus.RUNNING, None)],
        runner_records=records,
    )
    cancel_now(admin, finalize_run=False)
    try:
        updates = await execute_code(state, ctx)
        terminal = await persist_results(state, ctx)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()

    assert f"POST /runs/{runner_run_id}/cancel" in records
    assert updates["execution_result"].status is not RunStatus.SUCCEEDED
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    assert terminal["status"] == WorkflowStatus.CANCELLED.value
    assert _workflow(admin)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_persist_results_cancellation_outranks_a_stale_pipeline_error():
    admin, state, _ = seed_execution(status="cancelled")
    cancel_now(admin, finalize_run=False)
    ctx = make_ctx(admin, [])
    state["error"] = "OUT_OF_MEMORY: repair budget exhausted"
    try:
        updates = await persist_results(state, ctx)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert updates["status"] == WorkflowStatus.CANCELLED.value
    assert "error_code" not in updates
    assert _workflow(admin)["status"] == "cancelled"


# ------------------------------------------------------------- layer: endpoint


def _seed_wf(status="awaiting_spec_review", **overrides):
    admin = _seed()
    admin.tables["workflows"][0]["status"] = status
    admin.tables["workflows"][0].update(overrides)
    return admin


@pytest.fixture
def env(monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from reproagent_api.auth import get_anon_client
    from reproagent_api.main import create_app
    from reproagent_api.routers import papers
    from test_papers import FakeAuth

    def build(admin, arq=None):
        settings = ApiSettings(
            supabase_url="http://host.docker.internal:54321",
            supabase_anon_key="anon-test",
            supabase_service_role_key="service-test",
            _env_file=None,
        )
        monkeypatch.setattr(papers, "make_service_client", lambda s: admin)
        app = create_app(settings)
        app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
        with TestClient(app) as client:
            client.app.state.arq = arq if arq is not None else FakeArq()
            yield client

    return build


def test_cancel_endpoint_persists_intent_and_returns_current_state(env):
    admin = _seed_wf()
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"workflow_id": str(WORKFLOW_ID), "status": "cancelled"}
        assert admin.tables["workflows"][0]["status"] == "cancelled"
        assert admin.tables["workflows"][0]["error"].startswith("cancelled:")
        # cancellation never enqueues more work
        assert arq.jobs == []


def test_cancel_endpoint_is_idempotent(env):
    admin = _seed_wf()
    for client in env(admin):
        first = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        second = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        assert first.status_code == second.status_code == 200
        assert second.json()["status"] == "cancelled"
        assert len([r for r in admin.tables["workflows"] if r["status"] == "cancelled"]) == 1


@pytest.mark.parametrize(("status", "error"), [("completed", None), ("failed", "boom")])
def test_cancel_endpoint_never_rewrites_a_terminal_workflow(env, status, error):
    admin = _seed_wf(status, error=error)
    for client in env(admin):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["status"] == status
        assert admin.tables["workflows"][0]["status"] == status
        assert admin.tables["workflows"][0]["error"] == error


def test_cancel_endpoint_requires_ownership(env):
    admin = _seed_wf()
    admin.tables["workflows"][0]["projects"] = {"user_id": str(uuid4())}
    for client in env(admin):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        assert response.status_code == 404


def test_cancel_endpoint_cancels_the_active_run_and_runner(env):
    from test_runs import _seed_run

    from test_papers import USER_ID as API_USER_ID

    admin = _seed_wf("executing")
    run_id = _seed_run(admin, status="running")
    admin.tables["execution_runs"][0]["repair_attempt"] = 1
    admin.tables["execution_runs"][0]["experiments"]["projects"]["user_id"] = str(API_USER_ID)
    admin.tables["workflows"][0]["run_id"] = run_id
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cancelled.append(request.url.path.split("/")[2])
        return httpx.Response(404, json={"detail": "run not found"})

    for client in env(admin):
        client.app.state.runner_transport = httpx.MockTransport(handler)
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH)
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "cancelled"
        assert admin.tables["execution_runs"][0]["status"] == "cancelled"
        assert cancelled == [str(execution_request_id(UUID(run_id), 1))]


def test_resume_and_retry_refuse_a_cancelled_workflow(env):
    admin = _seed_wf()
    for client in env(admin):
        assert client.post(f"/v1/workflows/{WORKFLOW_ID}/cancel", headers=AUTH).status_code == 200
        assert client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", headers=AUTH, json={"stage": "spec"}
        ).status_code == 409
        assert client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH).status_code == 409


# ------------------------------------------------------------- layer: recovery


def test_resume_guard_only_allows_uncancelled_non_terminal_workflows():
    for status in ("cancelled", "completed", "failed"):
        assert worker.resume_allowed({"status": status}) is False
    for status in ("uploaded", "parsing", "awaiting_spec_review", "planning",
                   "awaiting_plan_approval", "generating_code", "validating_code", "executing",
                   "repairing", "evaluating"):
        assert worker.resume_allowed({"status": status}) is True
    # an unrecognised status is never evidence that a workflow may be driven
    assert worker.resume_allowed({"status": "queued"}) is False
    assert worker.resume_allowed({}) is False


def test_recover_task_never_drives_a_cancelled_workflow(monkeypatch):
    from contextlib import contextmanager

    admin = FakeAdmin(seed_workflow(), OWNERS)
    admin.tables["workflows"][0]["status"] = "cancelled"
    driven: list[str] = []
    workflow_id = admin.tables["workflows"][0]["id"]

    @contextmanager
    def _lock(settings, workflow_id):
        driven.append(workflow_id)
        yield True

    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "_drive_lock", _lock)
    asyncio.run(worker.recover_workflow_task(
        {"settings": ApiSettings(supabase_service_role_key="s", _env_file=None), "redis": None},
        workflow_id,
    ))
    assert driven == []
    assert admin.tables["workflows"][0]["status"] == "cancelled"

