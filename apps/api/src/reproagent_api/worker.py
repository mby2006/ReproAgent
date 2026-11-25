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
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from arq import cron
from arq.connections import RedisSettings
from reproagent_schemas import (
    BudgetConfig,
    RunStatus,
    WorkflowStatus,
)

from .clients.agent import AgentClient
from .clients.runner import RunnerClient
from .clients.supabase import make_service_client
from .config import ApiSettings
from .observability import configure_logging
from .services.comparisons import execute_comparison
from .services.dispatch import enqueue_pending_outbox
from .services.runs import finalize_execution_run, load_execution_plan
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

DISPATCH_STALE_SECONDS = 1260  # job_timeout (1200) + 60 seconds of grace
TERMINAL = {
    s.value for s in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED)
}
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
        logger.error("workflow %s not found", workflow_id)
        raise RuntimeError(f"Workflow {workflow_id} not found")
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
        logger.error("workflow %s not found", workflow_id)
        raise RuntimeError(f"Workflow {workflow_id} not found")
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


async def _recover_run_workflow(ctx: dict, admin, run: dict) -> bool:
    """Freshly recover an existing workflow, returning whether one was found."""
    workflows = admin.table("workflows").select("*").eq("run_id", run["id"]).execute().data
    if not workflows:
        return False
    await _recover_existing_workflow(ctx, admin, run, workflows[0])
    return True


async def _enqueue_workflow_recovery(redis, workflow_id: str) -> None:
    """Best-effort recovery delivery without a retained-result job identity."""
    if redis is None:
        return
    try:
        await redis.enqueue_job("recover_workflow_task", workflow_id)
    except Exception as exc:
        logger.warning("workflow %s recovery enqueue failed: %s", workflow_id, exc)


async def _recover_existing_workflow(ctx: dict, admin, run: dict, workflow: dict) -> None:
    """Recover the run's existing workflow instead of creating a duplicate."""
    if workflow["status"] not in TERMINAL:
        await _enqueue_workflow_recovery(ctx.get("redis"), workflow["id"])
        return

    if workflow["status"] == WorkflowStatus.CANCELLED.value:
        run_status = RunStatus.CANCELLED.value
        error = workflow.get("error")
        finalized = finalize_execution_run(
            admin,
            run["id"],
            RunStatus.CANCELLED,
            error,
            attempt=run.get("repair_attempt", 0),
        )
    else:
        run_status = (
            RunStatus.SUCCEEDED.value
            if workflow["status"] == WorkflowStatus.COMPLETED.value
            else RunStatus.FAILED.value
        )
        error = None
        if run_status == RunStatus.FAILED.value:
            error = workflow.get("error") or f"Workflow {workflow['id']} failed"
        finalized = admin.rpc("reconcile_execution_run", {
            "p_run_id": run["id"],
            "p_status": run_status,
            "p_error": error,
        })
    if finalized:
        logger.warning(
            "finalized run %s as %s from terminal workflow %s",
            run["id"], run_status, workflow["id"],
        )


async def run_experiment_task(ctx: dict, run_id: str) -> None:
    """Atomically claim a queued run, then drive its single deterministic workflow."""
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    claimed = admin.table("execution_runs").update({
        "status": RunStatus.DISPATCHING.value,
    }).eq("id", run_id).eq("status", RunStatus.QUEUED.value).execute().data
    if not claimed:
        existing = admin.table("execution_runs").select("id").eq("id", run_id).execute().data
        if not existing:
            logger.error("execution run %s not found", run_id)
            raise RuntimeError(f"Execution run {run_id} not found")
        # Another delivery owns the run, or it already reached a later state.
        return
    run = claimed[0]
    if await _recover_run_workflow(ctx, admin, run):
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
        }).eq("id", run_id).eq("status", RunStatus.DISPATCHING.value).execute()
        return

    workflow_id = uuid5(NAMESPACE_URL, f"reproagent:run:{run_id}")
    workflow_rows = (
        admin.table("workflows")
        .upsert(
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
            },
            # The deterministic primary key is inferable by PostgreSQL; the
            # partial workflows_run_id_unique index remains a second guard.
            on_conflict="id",
            ignore_duplicates=True,
        )
        .execute()
        .data
    )
    if not workflow_rows:
        if await _recover_run_workflow(ctx, admin, run):
            return
        raise RuntimeError(f"Workflow for execution run {run_id} disappeared after insert")
    workflow = workflow_rows[0]

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
    if workflow is None:
        logger.error("workflow %s not found", workflow_id)
        raise RuntimeError(f"Workflow {workflow_id} not found")
    if workflow["status"] in TERMINAL or workflow["status"] in AWAITING:
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


def _ensure_pending_outbox(admin, run_id: str, now: str) -> None:
    """Idempotently restore a run's dispatch intent after a lost delivery."""
    job_id = f"run-experiment:{run_id}"
    admin.table("job_outbox").upsert({
        "job_type": "run_experiment_task",
        "entity_id": run_id,
        "job_id": job_id,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }, on_conflict="job_id", ignore_duplicates=True).execute()
    admin.table("job_outbox").update({
        "status": "pending",
        "last_error": None,
        "updated_at": now,
    }).eq("job_id", job_id).neq("status", "pending").execute()


async def publish_worker_heartbeat(ctx: dict) -> None:
    redis = ctx.get("redis")
    if redis is not None:
        await redis.set("reproagent:worker:heartbeat", datetime.now(UTC).isoformat(), ex=60)


async def reconcile_dispatch_task(ctx: dict) -> None:
    """Recover lost dispatch intents and workflows stalled past the job timeout."""
    settings: ApiSettings = ctx["settings"]
    admin = make_service_client(settings)
    redis = ctx.get("redis")
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=DISPATCH_STALE_SECONDS)).isoformat()
    now_iso = now.isoformat()

    # An orphaned intent must never be sent to the worker, where it would fail
    # forever without a run row to claim.
    outbox_rows = admin.table("job_outbox").select("*").in_(
        "job_type", ["run_experiment_task"]
    ).in_("status", ["pending", "enqueued"]).execute().data
    entity_ids = [row["entity_id"] for row in outbox_rows]
    existing_run_ids: set[str] = set()
    if entity_ids:
        existing_run_ids = {
            row["id"] for row in admin.table("execution_runs").select("id").in_(
                "id", entity_ids
            ).execute().data
        }
    for row in outbox_rows:
        if row["entity_id"] in existing_run_ids:
            continue
        error = f"Execution run {row['entity_id']} no longer exists"
        logger.error("failing orphaned outbox job %s: %s", row["job_id"], error)
        admin.table("job_outbox").update({
            "status": "failed",
            "last_error": error,
            "updated_at": now_iso,
        }).eq("id", row["id"]).in_("status", ["pending", "enqueued"]).execute()

    stale_runs = admin.table("execution_runs").select("*").in_(
        "status", [RunStatus.QUEUED.value, RunStatus.DISPATCHING.value, RunStatus.RUNNING.value]
    ).lte("updated_at", cutoff).execute().data

    for run in stale_runs:
        run_id = run["id"]
        status = run["status"]
        if await _recover_run_workflow(ctx, admin, run):
            continue

        if status == RunStatus.QUEUED.value:
            _ensure_pending_outbox(admin, run_id, now_iso)
            continue

        if status == RunStatus.DISPATCHING.value:
            requeued = admin.table("execution_runs").update({
                "status": RunStatus.QUEUED.value,
                "updated_at": now_iso,
            }).eq("id", run_id).eq(
                "status", RunStatus.DISPATCHING.value
            ).lte("updated_at", cutoff).execute().data
            if requeued:
                logger.warning("requeued stale dispatching run %s", run_id)
                _ensure_pending_outbox(admin, run_id, now_iso)
        else:
            error = (
                "Stale running execution has no workflow after "
                f"{DISPATCH_STALE_SECONDS} seconds"
            )
            failed = admin.table("execution_runs").update({
                "status": RunStatus.FAILED.value,
                "error": error,
                "finished_at": now_iso,
                "updated_at": now_iso,
            }).eq("id", run_id).eq(
                "status", RunStatus.RUNNING.value
            ).lte("updated_at", cutoff).execute().data
            if failed:
                logger.error("failed stale running run %s: %s", run_id, error)

    await enqueue_pending_outbox(admin, redis)
    logger.info(
        "dispatch reconciliation complete",
        extra={
            "event": "dispatch_reconciled",
            "outbox_candidates": len(outbox_rows),
            "stale_runs": len(stale_runs),
        },
    )


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
        reconcile_dispatch_task,
        run_comparison_task,
    ]
    cron_jobs = [
        cron(reconcile_dispatch_task, second={0, 30}, run_at_startup=True),
        cron(publish_worker_heartbeat, second={0, 15, 30, 45}),
    ]
    max_tries = 1
    job_timeout = 1200
    # Each job can hold a 512MB+ sandbox; the default of 10 would OOM a small host.
    max_jobs = int(os.getenv("WORKER_MAX_JOBS", "10"))
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://127.0.0.1:6379"))

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        configure_logging("worker")
        settings = ApiSettings()
        ctx["settings"] = settings
        await publish_worker_heartbeat(ctx)
        saver, pool = await make_postgres_checkpointer(settings.supabase_db_url)
        ctx["checkpointer"] = saver
        ctx["checkpoint_pool"] = pool
        # Restart recovery: re-drive workflows that were mid-flight (not terminal,
        # not paused at a human gate). Awaiting workflows wait for user resume.
        admin = make_service_client(settings)
        redis = ctx.get("redis")
        await enqueue_pending_outbox(admin, redis)
        rows = admin.table("workflows").select("id,status").execute().data
        for row in rows:
            if row["status"] not in TERMINAL and row["status"] not in AWAITING and redis:
                await _enqueue_workflow_recovery(redis, row["id"])
        # Comparisons resume the same way: completed groups are skipped, so a
        # crash mid-pair only re-runs what is missing.
        for row in admin.table("comparisons").select("id,status").execute().data:
            if row["status"] in ("queued", "running") and redis:
                try:
                    await redis.enqueue_job("run_comparison_task", row["id"])
                except Exception as exc:
                    logger.warning("comparison %s recovery enqueue failed: %s", row["id"], exc)

    @staticmethod
    async def on_shutdown(ctx: dict) -> None:
        pool = ctx.get("checkpoint_pool")
        if pool is not None:
            await pool.close()
