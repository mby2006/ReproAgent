"""The 15 workflow nodes. Each delegates compute to the Agent/Runner over HTTP
and owns its database writes through the shared context. Expected failure
branches (invalid code, non-repairable error, exhausted budgets) route onward to
evaluation/persistence; unexpected exceptions propagate to the driver, which
finalizes the workflow as failed with the checkpointed state intact.
"""

import asyncio
import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

from reproagent_schemas import (
    ClassifyRequest,
    CodegenRequest,
    ErrorCategory,
    ErrorClassification,
    ExperimentPlan,
    ExtractRequest,
    GroundRequest,
    ParseRequest,
    PlanOrigin,
    PlanRequest,
    RepairRequest,
    ReproductionCard,
    RunResult,
    RunStatus,
    StaticValidateRequest,
    WorkflowStatus,
)

from ..services import (
    async_persist_result,
    build_evaluate_request,
    build_run_request,
    create_oom_plan,
    insert_code_version,
    insert_versioned,
    pin_run_files,
)
from .context import WorkflowContext
from .harness import ensure_llm_budget, record_usages, time_budget_exhausted

POLL_INTERVAL_SECONDS = 1.0
TERMINAL_RUN = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _finalize_run(admin, run_id: str, status: str, error: str | None = None) -> None:
    update = {"status": status, "finished_at": _now()}
    if error is not None:
        update["error"] = error[:2000]
    admin.table("execution_runs").update(update).eq("id", run_id).execute()


async def _poll_run(runner, runner_run_id: UUID):
    for _ in range(700):
        result = await runner.get_run(runner_run_id)
        if result.status in TERMINAL_RUN:
            return result
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return await runner.get_run(runner_run_id)


async def ingest_paper(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.PARSING, "ingest_paper")
    paper = (
        ctx.admin.table("papers").select("*").eq("id", state["paper_id"]).single().execute().data
    )
    pdf_bytes = ctx.admin.storage.from_("papers").download(paper["pdf_path"])
    return {
        **updates,
        "pdf_b64": base64.b64encode(pdf_bytes).decode(),
        "paper_title": paper["title"],
    }


async def extract_paper(state: dict, ctx: WorkflowContext) -> dict:
    response = await ctx.agent.parse_pdf(ParseRequest(pdf_b64=state["pdf_b64"]))
    return {"document": response.document}


async def build_reproduction_spec(state: dict, ctx: WorkflowContext) -> dict:
    ensure_llm_budget(state, ctx.budget_config)
    response = await ctx.agent.extract_spec(
        ExtractRequest(document=state["document"], title_hint=state.get("paper_title"))
    )
    budget = record_usages(state, ctx.budget_config, response.usages)
    return {"card_raw": response.card_raw, "usages": response.usages, "budget": budget}


async def validate_evidence(state: dict, ctx: WorkflowContext) -> dict:
    response = await ctx.agent.ground_spec(
        GroundRequest(card_raw=state["card_raw"], document=state["document"])
    )
    card = response.card
    latest = (
        ctx.admin.table("reproduction_specs")
        .select("id,version")
        .eq("paper_id", state["paper_id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    parent_id = latest[0]["id"] if latest else None

    def next_version() -> int:
        rows = (
            ctx.admin.table("reproduction_specs")
            .select("version")
            .eq("paper_id", state["paper_id"])
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["version"] + 1 if rows else 1

    def insert(v: int) -> dict:
        return (
            ctx.admin.table("reproduction_specs")
            .insert(
                {
                    "paper_id": state["paper_id"],
                    "version": v,
                    "origin": "extraction",
                    "parent_version_id": parent_id,
                    "spec_json": card.model_dump(mode="json"),
                    "status": "draft",
                }
            )
            .execute()
            .data[0]
        )

    spec = insert_versioned(next_version, insert)
    version = spec["version"]
    ctx.admin.table("papers").update({"status": "parsed"}).eq("id", state["paper_id"]).execute()
    return {"card": card, "spec_version_id": spec["id"], "spec_version": version}


async def human_review(state: dict, ctx: WorkflowContext) -> dict:
    from langgraph.types import interrupt

    updates = ctx.set_status(state, WorkflowStatus.AWAITING_SPEC_REVIEW, "human_review")
    interrupt({"stage": "spec", "spec_version_id": state.get("spec_version_id")})
    latest = (
        ctx.admin.table("reproduction_specs")
        .select("id,version,spec_json")
        .eq("paper_id", state["paper_id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data[0]
    )
    card = ReproductionCard.model_validate(latest["spec_json"])
    return {
        **updates,
        "card": card,
        "spec_version_id": latest["id"],
        "spec_version": latest["version"],
        "spec_confirmed": True,
    }


async def plan_experiment(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.PLANNING, "plan_experiment")
    response = await ctx.agent.plan(PlanRequest(card=state["card"]))
    plan = response.plan
    paper = (
        ctx.admin.table("papers")
        .select("title")
        .eq("id", state["paper_id"])
        .single()
        .execute()
        .data
    )
    experiment_id = uuid4()
    ctx.admin.table("experiments").insert(
        {
            "id": str(experiment_id),
            "project_id": state["project_id"],
            "paper_id": state["paper_id"],
            "spec_id": state["spec_version_id"],
            "name": f"{paper['title']} 复现"[:200],
            "status": "planning",
        }
    ).execute()
    plan_row = (
        ctx.admin.table("experiment_plans")
        .insert(
            {
                "experiment_id": str(experiment_id),
                "version": 1,
                "config_json": plan.model_dump(mode="json"),
                "budget_json": plan.runtime_budget.model_dump(mode="json"),
                "origin": PlanOrigin.GENERATED.value,
            }
        )
        .execute()
        .data[0]
    )
    return {
        **updates,
        "experiment_id": str(experiment_id),
        "plan": plan,
        "plan_id": plan_row["id"],
        "plan_version": 1,
    }


async def human_approve_plan(state: dict, ctx: WorkflowContext) -> dict:
    from langgraph.types import interrupt

    updates = ctx.set_status(state, WorkflowStatus.AWAITING_PLAN_APPROVAL, "human_approve_plan")
    interrupt({"stage": "plan", "experiment_id": state.get("experiment_id")})
    latest = (
        ctx.admin.table("experiment_plans")
        .select("id,version,config_json")
        .eq("experiment_id", state["experiment_id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data[0]
    )
    plan = ExperimentPlan.model_validate(latest["config_json"])
    return {
        **updates,
        "plan": plan,
        "plan_id": latest["id"],
        "plan_version": latest["version"],
        "plan_approved": True,
    }


async def generate_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.GENERATING_CODE, "generate_code")
    response = await ctx.agent.codegen(CodegenRequest(card=state["card"], plan=state["plan"]))
    bundle = response.bundle
    run_id = state.get("run_id")
    if not run_id:
        run_id = str(uuid4())
        ctx.admin.table("execution_runs").insert(
            {
                "id": run_id,
                "experiment_id": state["experiment_id"],
                "plan_id": state["plan_id"],
                "code_version": 1,
                "plan_version": state["plan_version"],
                "status": RunStatus.QUEUED.value,
                "repair_attempt": 0,
            }
        ).execute()
    return {**updates, "code_bundle": bundle, "code_version": 1, "run_id": run_id}


_STATIC_SECURITY_CODES = {
    "banned_import",
    "banned_attr",
    "banned_call",
    "network_download",
    "sensitive_path",
}


async def validate_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.VALIDATING_CODE, "validate_code")
    report = await ctx.agent.validate_code(StaticValidateRequest(bundle=state["code_bundle"]))
    if report.ok:
        return {**updates, "validation_ok": True, "error": None}
    issues = "; ".join(i.message for i in report.issues)
    error = f"static validation failed: {issues}"
    # Security violations are never auto-repaired; structural issues re-render
    # the canonical templates via the shared repair loop.
    security = any(i.code in _STATIC_SECURITY_CODES for i in report.issues)
    classification = ErrorClassification(
        category=ErrorCategory.UNKNOWN_ERROR if security else ErrorCategory.SYNTAX_ERROR,
        reason=error,
        repairable=not security,
    )
    ctx.admin.table("execution_runs").update(
        {"error_category": classification.category.value}
    ).eq("id", state["run_id"]).execute()
    return {
        **updates,
        "validation_ok": False,
        "classification": classification,
        "error": error,
    }


async def execute_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.EXECUTING, "execute_code")
    run_id = state["run_id"]
    if time_budget_exhausted(state, ctx.budget_config):
        error = "experiment time budget exhausted"
        _finalize_run(ctx.admin, run_id, RunStatus.FAILED.value, error)
        return {**updates, "budget_exhausted": True, "error": error}

    # A cancel can land while the run is still queued; re-check the persisted
    # status before driving the Runner so RUNNING never overwrites CANCELLED.
    current = ctx.admin.table("execution_runs").select("status").eq("id", run_id).execute().data
    if current and current[0]["status"] == RunStatus.CANCELLED.value:
        error = "cancelled before execution started"
        _finalize_run(ctx.admin, run_id, RunStatus.CANCELLED.value, error)
        result = RunResult(run_id=UUID(run_id), status=RunStatus.CANCELLED, error=error)
        return {**updates, "execution_result": result}

    attempt = state.get("repair_attempt", 0)
    bundle = state["code_bundle"]
    change_reason = (
        "initial deterministic codegen" if attempt == 0 else f"auto-repair attempt {attempt}"
    )
    related_error = None
    if attempt > 0:
        if state.get("execution_result") is not None:
            related_error = state["execution_result"].error
        elif state.get("classification") is not None:
            related_error = state["classification"].reason
    code_version, file_ids = insert_code_version(
        ctx.admin,
        state["experiment_id"],
        state["code_version"],
        bundle,
        change_reason,
        related_error,
    )
    pin_run_files(ctx.admin, run_id, file_ids)
    ctx.admin.table("execution_runs").update(
        {
            "code_version": code_version,
            "code_hash": bundle.code_hash,
            "repair_attempt": attempt,
            "plan_id": state["plan_id"],
            "plan_version": state["plan_version"],
            "status": RunStatus.RUNNING.value,
            "started_at": _now(),
        }
    ).eq("id", run_id).execute()

    request = build_run_request(
        UUID(state["experiment_id"]),
        UUID(state["plan_id"]),
        bundle,
        state["plan"],
        code_version,
        state["plan_version"],
    )
    accepted = await ctx.runner.start_run(request)
    runner_run_id = accepted.run_id
    ctx.admin.table("execution_runs").update({"runner_run_id": str(runner_run_id)}).eq(
        "id", run_id
    ).execute()

    result = await _poll_run(ctx.runner, runner_run_id)
    await async_persist_result(ctx.admin, run_id, runner_run_id, result, ctx.runner, attempt)
    _finalize_run(ctx.admin, run_id, result.status.value, result.error)

    budget = state["budget"].model_copy(deep=True)
    budget.record_experiment_time(result.execution_time or 0.0)
    return {
        **updates,
        "execution_result": result,
        "runner_run_id": str(runner_run_id),
        "code_version": code_version,
        "budget": budget,
    }


async def classify_error(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.REPAIRING, "classify_error")
    result = state["execution_result"]
    classification = await ctx.agent.classify_error(
        ClassifyRequest(
            stderr=result.stderr,
            stdout=result.stdout,
            exit_code=result.exit_code,
            status=result.status,
        )
    )
    ctx.admin.table("execution_runs").update({"error_category": classification.category.value}).eq(
        "id", state["run_id"]
    ).execute()
    return {**updates, "classification": classification}


async def repair_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.REPAIRING, "repair_code")
    classification = state["classification"]
    result = state.get("execution_result")
    repair = await ctx.agent.repair_code(
        RepairRequest(
            bundle=state["code_bundle"], classification=classification, plan=state["plan"]
        )
    )
    budget = state["budget"].model_copy(deep=True)
    budget.repair_count += 1
    plan = state["plan"]
    plan_id = state["plan_id"]
    plan_version = state["plan_version"]
    bundle = repair.bundle
    if repair.replan_required:
        plan_row = (
            ctx.admin.table("experiment_plans")
            .select("*")
            .eq("id", plan_id)
            .single()
            .execute()
            .data
        )
        new_row = create_oom_plan(
            ctx.admin,
            state["experiment_id"],
            plan_row,
            UUID(state["run_id"]),
            (result.error if result is not None else None) or classification.reason,
        )
        plan = ExperimentPlan.model_validate(new_row["config_json"])
        plan_id = new_row["id"]
        plan_version = new_row["version"]
        budget.replan_count += 1
        bundle = (await ctx.agent.codegen(CodegenRequest(card=state["card"], plan=plan))).bundle
    return {
        **updates,
        "code_bundle": bundle,
        "code_version": state["code_version"] + 1,
        "repair_attempt": state.get("repair_attempt", 0) + 1,
        "plan": plan,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "budget": budget,
    }


async def evaluate_result(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.EVALUATING, "evaluate_result")
    request = build_evaluate_request(ctx.admin, state["run_id"])
    response = await ctx.agent.evaluate(request)
    return {**updates, "report": response.report, "markdown": response.markdown}


async def generate_report(state: dict, ctx: WorkflowContext) -> dict:
    row = {
        "run_id": state["run_id"],
        "report_json": state["report"].model_dump(mode="json"),
        "markdown": state["markdown"],
    }
    existing = (
        ctx.admin.table("reproduction_reports")
        .select("id")
        .eq("run_id", state["run_id"])
        .execute()
        .data
    )
    if existing:
        ctx.admin.table("reproduction_reports").update(row).eq("run_id", state["run_id"]).execute()
    else:
        ctx.admin.table("reproduction_reports").insert(row).execute()
    return {}


async def persist_results(state: dict, ctx: WorkflowContext) -> dict:
    pipeline_failed = bool(state.get("error")) or state.get("validation_ok") is False
    status = WorkflowStatus.FAILED if pipeline_failed else WorkflowStatus.COMPLETED
    if state.get("validation_ok") is False and state.get("run_id"):
        _finalize_run(ctx.admin, state["run_id"], RunStatus.FAILED.value, state.get("error"))
    if state.get("experiment_id"):
        result = state.get("execution_result")
        if pipeline_failed or result is None:
            experiment_status = "failed"
        elif result.status is RunStatus.SUCCEEDED:
            experiment_status = "succeeded"
        elif result.status is RunStatus.CANCELLED:
            experiment_status = "cancelled"
        else:
            experiment_status = "failed"
        ctx.admin.table("experiments").update({"status": experiment_status}).eq(
            "id", state["experiment_id"]
        ).execute()
    ctx.finalize_workflow(state, status, state.get("error"))
    return {"status": status.value, "current_node": "persist_results"}
