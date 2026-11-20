"""Worker task tests: graph-driven start/resume/recover wired through arq ctx.

Uses MemorySaver + FakeAdmin + stub transports. Verifies the worker entry points
drive the unified graph, that restart recovery skips terminal/awaiting workflows,
starts uncheckpointed ones, and continues mid-flight ones from their checkpoint.
"""

from uuid import uuid4

import pytest
from reproagent_agent.planning import build_plan
from reproagent_api import worker
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import build_workflow_graph, make_memory_checkpointer
from reproagent_schemas import BudgetConfig, RunStatus, WorkflowStatus
from test_runs import (
    EXPERIMENT_ID,
    PLAN_ID,
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    make_card,
    make_result,
    runner_transport,
    seed_tables,
)
from test_workflow_graph import (
    PAPER_ID,
    THREAD_ID,
    WORKFLOW_ID,
    _state,
    seed_workflow,
    workflow_agent_transport,
)


def _ctx(admin, results, saver, monkeypatch):
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    admin.storage.files["u/p.pdf"] = b"%PDF-1.4 fake"
    return {
        "settings": ApiSettings(
            supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
        ),
        "agent_transport": workflow_agent_transport(),
        "runner_transport": runner_transport(results),
        "checkpointer": saver,
    }


@pytest.mark.asyncio
async def test_run_workflow_task_pauses_at_spec_review(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(
        admin, [make_result(RunStatus.SUCCEEDED, 0)], make_memory_checkpointer(), monkeypatch
    )
    await worker.run_workflow_task(ctx, str(WORKFLOW_ID))
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value
    assert len(admin.tables["reproduction_specs"]) == 1
    assert admin.tables["experiments"] == []


@pytest.mark.asyncio
async def test_resume_tasks_complete_the_workflow(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    saver = make_memory_checkpointer()
    ctx = _ctx(
        admin,
        [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.98})],
        saver,
        monkeypatch,
    )
    await worker.run_workflow_task(ctx, str(WORKFLOW_ID))
    admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
    await worker.resume_workflow_task(ctx, str(WORKFLOW_ID), "spec")
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.AWAITING_PLAN_APPROVAL.value
    admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
    await worker.resume_workflow_task(ctx, str(WORKFLOW_ID), "plan")
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.COMPLETED.value
    assert admin.tables["execution_runs"][0]["status"] == "succeeded"
    assert len(admin.tables["reproduction_reports"]) == 1


@pytest.mark.asyncio
async def test_run_experiment_task_creates_workflow(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = str(uuid4())
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": str(EXPERIMENT_ID),
            "plan_id": str(PLAN_ID),
            "code_version": 1,
            "plan_version": 1,
            "status": "queued",
            "repair_attempt": 0,
        }
    )
    ctx = _ctx(
        admin,
        [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})],
        make_memory_checkpointer(),
        monkeypatch,
    )
    await worker.run_experiment_task(ctx, run_id)
    assert admin.tables["execution_runs"][0]["status"] == "succeeded"
    assert len(admin.tables["workflows"]) == 1
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.COMPLETED.value
    assert admin.tables["workflows"][0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_repeat_runs_allocate_new_code_versions(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    saver = make_memory_checkpointer()
    for _ in range(2):
        run_id = str(uuid4())
        admin.tables["execution_runs"].append(
            {
                "id": run_id,
                "experiment_id": str(EXPERIMENT_ID),
                "plan_id": str(PLAN_ID),
                "code_version": 1,
                "plan_version": 1,
                "status": "queued",
                "repair_attempt": 0,
            }
        )
        ctx = _ctx(
            admin,
            [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})],
            saver,
            monkeypatch,
        )
        await worker.run_experiment_task(ctx, run_id)
    assert [r["status"] for r in admin.tables["execution_runs"]] == ["succeeded", "succeeded"]
    assert [r["code_version"] for r in admin.tables["execution_runs"]] == [1, 2]
    assert sorted({r["version"] for r in admin.tables["generated_files"]}) == [1, 2]


@pytest.mark.asyncio
async def test_recovery_skips_terminal_and_awaiting(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(admin, [], make_memory_checkpointer(), monkeypatch)
    for status in (WorkflowStatus.COMPLETED.value, WorkflowStatus.AWAITING_SPEC_REVIEW.value):
        admin.tables["workflows"][0]["status"] = status
        await worker.recover_workflow_task(ctx, str(WORKFLOW_ID))
        assert admin.tables["workflows"][0]["status"] == status  # untouched
    assert admin.tables["experiments"] == []


@pytest.mark.asyncio
async def test_recovery_starts_uncheckpointed_workflow(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(
        admin, [make_result(RunStatus.SUCCEEDED, 0)], make_memory_checkpointer(), monkeypatch
    )
    admin.tables["workflows"][0]["status"] = WorkflowStatus.UPLOADED.value
    await worker.recover_workflow_task(ctx, str(WORKFLOW_ID))
    # no checkpoint existed -> started fresh -> paused at the first human gate
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value


@pytest.mark.asyncio
async def test_recovery_continues_midflight_from_checkpoint(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    saver = make_memory_checkpointer()
    result = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})
    ctx = _ctx(admin, [result], saver, monkeypatch)

    # Seed a checkpoint as if plan approval just completed, then "crash" before
    # codegen runs. Status is non-terminal/non-awaiting -> recovery must continue.
    admin.tables["workflows"][0]["status"] = WorkflowStatus.GENERATING_CODE.value
    card = make_card()
    plan = build_plan(card)
    experiment_id = str(uuid4())
    plan_id = str(uuid4())
    spec_id = str(uuid4())
    admin.tables["reproduction_specs"].append(
        {
            "id": spec_id,
            "paper_id": str(PAPER_ID),
            "version": 1,
            "status": "confirmed",
            "spec_json": card.model_dump(mode="json"),
        }
    )
    admin.tables["experiments"].append(
        {
            "id": experiment_id,
            "project_id": str(PROJECT_ID),
            "paper_id": str(PAPER_ID),
            "spec_id": spec_id,
            "status": "approved",
            "projects": {"user_id": str(USER_ID)},
        }
    )
    admin.tables["experiment_plans"].append(
        {
            "id": plan_id,
            "experiment_id": experiment_id,
            "version": 1,
            "config_json": plan.model_dump(mode="json"),
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    state = _state(admin)
    state["status"] = WorkflowStatus.GENERATING_CODE.value
    state.update(
        card=card, plan=plan, experiment_id=experiment_id, plan_id=plan_id, plan_version=1,
        spec_version_id=spec_id, spec_version=1,
    )

    from reproagent_api.clients.agent import AgentClient
    from reproagent_api.clients.runner import RunnerClient
    from reproagent_api.workflow import WorkflowContext

    settings = ctx["settings"]
    seed_ctx = WorkflowContext(
        admin=admin,
        agent=AgentClient(settings, transport=workflow_agent_transport()),
        runner=RunnerClient(settings, transport=runner_transport([result])),
        settings=settings,
    )
    graph = build_workflow_graph(seed_ctx, saver)
    graph.update_state(
        {"configurable": {"thread_id": THREAD_ID}}, state, as_node="human_approve_plan"
    )
    await seed_ctx.agent.aclose()
    await seed_ctx.runner.aclose()

    await worker.recover_workflow_task(ctx, str(WORKFLOW_ID))
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.COMPLETED.value
    assert admin.tables["execution_runs"][0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_recovery_redrives_persisted_failure(monkeypatch):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    saver = make_memory_checkpointer()
    failed = make_result(RunStatus.FAILED, 1, stderr="ModuleNotFoundError: No module named 'x'")
    # A repairable failure whose run already burned the whole time budget: the
    # next execute_code persists budget exhaustion and the graph reaches END in
    # FAILED state with no pending node — a plain continue would silently no-op.
    with monkeypatch.context() as m:
        m.setattr(worker, "BudgetConfig", lambda: BudgetConfig(max_experiment_seconds=1))
        ctx = _ctx(admin, [failed], saver, m)
        await worker.run_workflow_task(ctx, str(WORKFLOW_ID))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await worker.resume_workflow_task(ctx, str(WORKFLOW_ID), "spec")
        admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
        await worker.resume_workflow_task(ctx, str(WORKFLOW_ID), "plan")
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.FAILED.value
    assert admin.tables["execution_runs"][0]["status"] == "failed"

    # Retry claim flips failed -> executing; recovery must re-drive from codegen
    # with a fresh run row and budget on the same thread.
    admin.tables["workflows"][0]["status"] = WorkflowStatus.EXECUTING.value
    ctx2 = _ctx(
        admin,
        [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})],
        saver,
        monkeypatch,
    )
    await worker.recover_workflow_task(ctx2, str(WORKFLOW_ID))
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.COMPLETED.value
    assert [r["status"] for r in admin.tables["execution_runs"]] == ["failed", "succeeded"]
    assert admin.tables["workflows"][0]["run_id"] == admin.tables["execution_runs"][1]["id"]
