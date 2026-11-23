"""arq worker: the unified LangGraph workflow is the single orchestration truth.

The worker lives in the API trust domain (it needs Supabase keys); the agent
stays key-scoped to the LLM and the Runner holds no keys. Every node delegates
compute over HTTP and owns its DB writes through the workflow context. State is
checkpointed to Supabase Postgres (thread_id == workflow_id) so human gates and
crashes resume from the last completed node.

Entry points:
- run_workflow_task: drive the full 15-node graph from upload.
- resume_workflow_task: continue past a human gate (spec / plan confirmation).
- run_experiment_task: legacy runs-router entry; seeds the SAME graph at the
  execution phase for an already-approved plan (no separate imperative loop).
- recover_workflow_task: restart recovery for interrupted, non-awaiting workflows.
"""

import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from arq.connections import RedisSettings
from reproagent_schemas import (
    BudgetConfig,
    WorkflowStatus,
)

from .clients.agent import AgentClient
from .clients.runner import RunnerClient
from .clients.supabase import make_service_client
from .config import ApiSettings
from .services.comparisons import execute_comparison
from .services.runs import load_execution_plan
from .workflow import (
    WorkflowContext,
    build_workflow_graph,
    continue_workflow,
    drive_from_execution,
    initial_state,
    make_postgres_checkpointer,
    resume_workflow,
    start_workflow,
)

logger = logging.getLogger(__name__)

TERMINAL = {s.value for s in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED)}
AWAITING = {
    s.value for s in (WorkflowStatus.AWAITING_SPEC_REVIEW, WorkflowStatus.AWAITING_PLAN_APPROVAL)
}


def _advisory_key(workflow_id: str) -> int:
    return int(UUID(workflow_id)) & 0x7FFFFFFFFFFFFFFF


@contextmanager
def _drive_lock(settings: ApiSettings, workflow_id: str):
    """Non-blocking, session-scoped mutex: only one job may drive a thread at a time.

    Restart recovery can enqueue recover_workflow_task for a workflow whose original
    run_workflow_task is still queued/running; without this lock both drive the same
    thread_id and corrupt the shared checkpoint (duplicate nodes, versions, forked
    checkpoint chains). pg_try_advisory_lock returns immediately so the loser skips.
    The lock belongs to this connection's session, so a worker crash drops it and
    recovery can take over without guessing a TTL. Sync psycopg keeps the brief
    acquire/release independent of the event loop (Windows Proactor included); the
    connection then idle-holds the session lock for the duration of the drive.
    """
    conn = psycopg.connect(settings.supabase_db_url, autocommit=True)
    try:
        row = conn.execute(
            "select pg_try_advisory_lock(%s)", (_advisory_key(workflow_id),)
        ).fetchone()
        yield bool(row and row[0])
    finally:
        conn.close()


async def _get_checkpointer(ctx: dict, settings: ApiSettings):
    if "checkpointer" not in ctx:
        saver, pool = await make_postgres_checkpointer(settings.supabase_db_url)
        ctx["checkpointer"] = saver
        ctx["checkpoint_pool"] = pool
    return ctx["checkpointer"]


def _build_context(ctx: dict, admin, settings: ApiSettings) -> WorkflowContext:
    return WorkflowContext(
        admin=admin,
        agent=AgentClient(settings, transport=ctx.get("agent_transport")),
        runner=RunnerClient(settings, transport=ctx.get("runner_transport")),
        settings=settings,
        budget_config=BudgetConfig(),
    )


def _load_workflow(admin, workflow_id: str) -> dict | None:
    rows = admin.table("workflows").select("*").eq("id", workflow_id).execute().data
    return rows[0] if rows else None


async def run_workflow_task(ctx: dict, workflow_id: str) -> None:
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    workflow = _load_workflow(admin, workflow_id)
    if workflow is None:
        return
    with _drive_lock(settings, workflow_id) as acquired:
        if not acquired:
            logger.info("workflow %s: another driver holds the thread; skipping", workflow_id)
            return
        wctx = _build_context(ctx, admin, settings)
        checkpointer = await _get_checkpointer(ctx, settings)
        try:
            await start_workflow(wctx, checkpointer, initial_state(workflow, wctx.budget_config))
        finally:
            await wctx.agent.aclose()
            await wctx.runner.aclose()


async def resume_workflow_task(ctx: dict, workflow_id: str, stage: str) -> None:
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    workflow = _load_workflow(admin, workflow_id)
    if workflow is None:
        return
    # Guard against a stale/duplicate resume driving a thread that already moved
    # past its gate: only the matching awaiting state may be resumed.
    expected = (
        WorkflowStatus.AWAITING_SPEC_REVIEW.value
        if stage == "spec"
        else WorkflowStatus.AWAITING_PLAN_APPROVAL.value
    )
    if workflow["status"] != expected:
        return
    with _drive_lock(settings, workflow_id) as acquired:
        if not acquired:
            logger.info("workflow %s: another driver holds the thread; skipping", workflow_id)
            return
        wctx = _build_context(ctx, admin, settings)
        checkpointer = await _get_checkpointer(ctx, settings)
        fallback = initial_state(workflow, wctx.budget_config)
        try:
            await resume_workflow(
                wctx, checkpointer, workflow["thread_id"], {"stage": stage}, fallback
            )
        finally:
            await wctx.agent.aclose()
            await wctx.runner.aclose()


async def run_experiment_task(ctx: dict, run_id: str) -> None:
    """Legacy runs-router entry: drive the unified graph from generate_code onward."""
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    runs = admin.table("execution_runs").select("*").eq("id", run_id).execute().data
    if not runs:
        return
    run = runs[0]
    if run["status"] != "queued":
        return
    try:
        experiment, plan_row, plan, spec, card, _ = load_execution_plan(
            admin, run["experiment_id"], run["plan_id"],
        )
        if run["plan_version"] != plan_row["version"]:
            raise ValueError("Queued plan version does not match the selected plan")
    except ValueError:
        admin.table("execution_runs").update({
            "status": "failed", "finished_at": datetime.now(UTC).isoformat(),
            "error": "Execution authorization invalid; approve the latest plan before retrying",
        }).eq("id", run_id).eq("status", "queued").execute()
        return

    workflow_id = uuid4()
    workflow = (
        admin.table("workflows")
        .insert(
            {
                "id": str(workflow_id),
                "thread_id": str(workflow_id),
                "project_id": experiment["project_id"],
                "paper_id": experiment["paper_id"],
                "status": WorkflowStatus.EXECUTING.value,
                "experiment_id": str(experiment["id"]),
                "run_id": run_id,
                "spec_version_id": spec["id"],
                "plan_id": plan_row["id"],
                "budget_json": {},
            }
        )
        .execute()
        .data[0]
    )

    with _drive_lock(settings, workflow["id"]) as acquired:
        if not acquired:
            logger.info("workflow %s: another driver holds the thread; skipping", workflow["id"])
            return
        wctx = _build_context(ctx, admin, settings)
        checkpointer = await _get_checkpointer(ctx, settings)
        state = initial_state(workflow, wctx.budget_config)
        state.update(
            card=card,
            plan=plan,
            experiment_id=str(experiment["id"]),
            run_id=run_id,
            plan_id=plan_row["id"],
            plan_version=plan_row["version"],
            spec_version_id=spec["id"],
            spec_version=spec["version"],
            status=WorkflowStatus.EXECUTING.value,
        )
        try:
            await drive_from_execution(wctx, checkpointer, state)
        finally:
            await wctx.agent.aclose()
            await wctx.runner.aclose()


def _recovery_execution_state(admin, workflow: dict, budget_config: BudgetConfig):
    """Rebuild execution-phase state for a thread that already reached END.

    A persisted failure (unlike a node that raised) leaves no pending node, so
    retry re-enters the graph at generate_code with a fresh run row instead of
    silently no-op'ing on the finished checkpoint.
    """
    experiment_id = workflow.get("experiment_id")
    if not experiment_id:
        return None
    _, plan_row, plan, spec, card, _ = load_execution_plan(
        admin, experiment_id, project_id=workflow["project_id"], paper_id=workflow["paper_id"],
        spec_id=workflow.get("spec_version_id"),
    )
    admin.table("experiments").update({"status": "running"}).eq("id", experiment_id).execute()
    state = initial_state(workflow, budget_config)
    state.update(
        card=card,
        plan=plan,
        experiment_id=experiment_id,
        run_id=None,
        plan_id=plan_row["id"],
        plan_version=plan_row["version"],
        spec_version_id=spec["id"],
        spec_version=spec["version"],
        status=WorkflowStatus.EXECUTING.value,
        error=None,
        execution_result=None,
        classification=None,
        budget_exhausted=False,
    )
    return state


async def recover_workflow_task(ctx: dict, workflow_id: str) -> None:
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    workflow = _load_workflow(admin, workflow_id)
    if workflow is None or workflow["status"] in TERMINAL or workflow["status"] in AWAITING:
        return
    with _drive_lock(settings, workflow_id) as acquired:
        if not acquired:
            logger.info("workflow %s: another driver holds the thread; skipping", workflow_id)
            return
        wctx = _build_context(ctx, admin, settings)
        checkpointer = await _get_checkpointer(ctx, settings)
        fallback = initial_state(workflow, wctx.budget_config)
        graph = build_workflow_graph(wctx, checkpointer)
        snapshot = await graph.aget_state({"configurable": {"thread_id": workflow["thread_id"]}})
        try:
            if snapshot.values and snapshot.next:
                await continue_workflow(wctx, checkpointer, workflow["thread_id"], fallback)
            elif snapshot.values:
                try:
                    state = _recovery_execution_state(admin, workflow, wctx.budget_config)
                except ValueError:
                    wctx.finalize_workflow(
                        fallback, WorkflowStatus.FAILED,
                        "Execution authorization invalid; approve the latest plan before retrying",
                    )
                    return
                if state is not None:
                    await drive_from_execution(wctx, checkpointer, state)
                else:
                    await start_workflow(wctx, checkpointer, fallback)
            else:
                await start_workflow(wctx, checkpointer, fallback)
        finally:
            await wctx.agent.aclose()
            await wctx.runner.aclose()


async def run_comparison_task(ctx: dict, comparison_id: str) -> None:
    """Drive a paired comparison: paired runs, verification, report.

    Idempotent by construction: an advisory lock guarantees a single driver per
    comparison (duplicate submits, restart recovery), and groups that already
    have a succeeded -- or still in-flight -- run are reused instead of retrained.
    """
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    with _drive_lock(settings, comparison_id) as acquired:
        if not acquired:
            logger.info(
                "comparison %s: another driver holds the thread; skipping", comparison_id
            )
            return
        agent = AgentClient(settings, transport=ctx.get("agent_transport"))
        runner = RunnerClient(settings, transport=ctx.get("runner_transport"))
        try:
            await execute_comparison(admin, agent, runner, comparison_id)
        finally:
            await agent.aclose()
            await runner.aclose()


class WorkerSettings:
    functions = [
        run_workflow_task,
        resume_workflow_task,
        run_experiment_task,
        recover_workflow_task,
        run_comparison_task,
    ]
    max_tries = 1
    job_timeout = 1200
    # Each job can hold a 512MB+ sandbox; the default of 10 would OOM a small host.
    max_jobs = int(os.getenv("WORKER_MAX_JOBS", "10"))
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://127.0.0.1:6379"))

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        settings = ApiSettings()
        ctx["settings"] = settings
        saver, pool = await make_postgres_checkpointer(settings.supabase_db_url)
        ctx["checkpointer"] = saver
        ctx["checkpoint_pool"] = pool
        # Restart recovery: re-drive workflows that were mid-flight (not terminal,
        # not paused at a human gate). Awaiting workflows wait for user resume.
        admin = make_service_client(settings)
        rows = admin.table("workflows").select("id,status").execute().data
        redis = ctx.get("redis")
        for row in rows:
            if row["status"] not in TERMINAL and row["status"] not in AWAITING and redis:
                await redis.enqueue_job("recover_workflow_task", row["id"])
        # Comparisons resume the same way: completed groups are skipped, so a
        # crash mid-pair only re-runs what is missing.
        for row in admin.table("comparisons").select("id,status").execute().data:
            if row["status"] in ("queued", "running") and redis:
                await redis.enqueue_job("run_comparison_task", row["id"])

    @staticmethod
    async def on_shutdown(ctx: dict) -> None:
        pool = ctx.get("checkpoint_pool")
        if pool is not None:
            await pool.close()
