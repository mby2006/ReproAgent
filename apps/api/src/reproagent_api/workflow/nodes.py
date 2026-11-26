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
    ConfirmationSource,
    ErrorCategory,
    ErrorClassification,
    ExperimentPlan,
    ExtractRequest,
    GroundRequest,
    ParseRequest,
    PlanOrigin,
    PlanRequest,
    RepairRequest,
    ReviewMode,
    ReviewRequest,
    RunResult,
    RunStatus,
    StaticValidateRequest,
    WorkflowStatus,
)

from ..services.review import validate_review_response
from ..services.runs import (
    async_persist_result,
    build_run_request,
    create_oom_plan,
    finalize_execution_run,
    insert_code_version,
    load_confirmed_card,
    load_execution_plan,
    load_run_row,
    mark_attempt_running,
    persist_runner_run_id,
    pin_run_files,
    terminal_result_from_row,
)
from ..services.versions import insert_versioned
from .context import WorkflowContext
from .harness import BudgetExceeded, ensure_llm_budget, record_usages, time_budget_exhausted

POLL_INTERVAL_SECONDS = 1.0
TERMINAL_RUN = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _finalize_run(
    admin, run_id: str, status: str, error: str | None = None, *, attempt: int | None = None
) -> None:
    finalize_execution_run(admin, run_id, RunStatus(status), error, attempt=attempt)


async def _poll_run(runner, runner_run_id: UUID):
    for _ in range(700):
        result = await runner.get_run(runner_run_id)
        if result.status in TERMINAL_RUN:
            return result
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    try:
        await runner.cancel_run(runner_run_id)
    except Exception:
        pass
    return RunResult(
        run_id=runner_run_id,
        status=RunStatus.TIMEOUT,
        error="Runner polling budget exhausted; execution was cancelled",
        error_category=ErrorCategory.TIMEOUT,
    )


def _reuse_code_version(admin, state: dict, row: dict, bundle, attempt: int):
    if (
        row.get("code_hash") != bundle.code_hash
        or row.get("repair_attempt", 0) != attempt
        or not row.get("code_version")
    ):
        return None
    pins = admin.table("execution_run_files").select("generated_file_id").eq(
        "run_id", state["run_id"]
    ).execute().data
    if pins:
        rows = admin.table("generated_files").select("*").in_(
            "id", [pin["generated_file_id"] for pin in pins]
        ).execute().data
    else:
        rows = admin.table("generated_files").select("*").eq(
            "experiment_id", state["experiment_id"]
        ).eq("version", row["code_version"]).execute().data
    if {item["path"] for item in rows} != {item.path for item in bundle.files}:
        return None
    return row["code_version"], [item["id"] for item in rows]


async def _cancel_runner(runner, runner_run_id: UUID) -> None:
    try:
        await runner.cancel_run(runner_run_id)
    except Exception:
        pass


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
    document = response.document
    # Hard gate: reject scanned PDFs immediately before any further processing.
    if document.ocr_required_pages:
        error = "SCANNED_PDF: 扫描 PDF 需要 OCR，当前版本不支持"
        return {"document": document, "error": error, "support_code": "SCANNED_PDF"}
    return {"document": document}


async def build_reproduction_spec(state: dict, ctx: WorkflowContext) -> dict:
    ensure_llm_budget(state, ctx.budget_config)
    response = await ctx.agent.extract_spec(
        ExtractRequest(document=state["document"], title_hint=state.get("paper_title"))
    )
    budget = record_usages(state, ctx.budget_config, response.usages)
    # Hard gate: validate the extracted card against supported scope.
    card_raw = response.card_raw
    try:
        from reproagent_schemas import ReproductionCard

        card = ReproductionCard.model_validate(card_raw)
        support = await ctx.agent.check_support(state["document"], card)
        if not support.supported:
            error = f"{support.code}: {support.message}"
            return {
                "card_raw": card_raw,
                "usages": response.usages,
                "budget": budget,
                "error": error,
                "support_code": support.code,
            }
    except Exception:
        # If card validation fails, let the downstream ground_spec handle it.
        pass
    return {"card_raw": card_raw, "usages": response.usages, "budget": budget}


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
    latest, card, _ = load_confirmed_card(ctx.admin, state["project_id"], state["paper_id"])
    return {
        **updates,
        "card": card,
        "spec_version_id": latest["id"],
        "spec_version": latest["version"],
        "spec_confirmed": True,
    }


async def plan_experiment(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.PLANNING, "plan_experiment")
    _, card, actor = load_confirmed_card(
        ctx.admin, state["project_id"], state["paper_id"], state["spec_version_id"],
    )
    response = await ctx.agent.plan(PlanRequest(card=card))
    # Only the persisted Card can authorize review; planner output cannot.
    data = response.plan.model_dump(mode="json")
    delegated = card.review_mode is ReviewMode.DELEGATED
    data.update(review_mode=ReviewMode.ASSISTED if delegated else card.review_mode,
                delegated_at=None, delegated_by=None)
    for assumption in data["assumptions"]:
        if assumption.get("confirmed_by") == ConfirmationSource.AUTO:
            assumption.update(confirmed_by=None, human_confirmed=False, requires_confirmation=True)
    for param in data["training_config"].values():
        if isinstance(param, dict) and param.get("confirmed_by") == ConfirmationSource.AUTO:
            param.update(confirmed_by=None, human_confirmed=False, requires_confirmation=True)
    plan = ExperimentPlan.model_validate(data)
    records = []
    usages = []
    budget = state["budget"]
    if delegated:
        from ..services.plan_edits import apply_delegated_review

        ensure_llm_budget(state, ctx.budget_config)
        context = ReviewRequest(card=card, plan=plan, scope=str(actor))
        try:
            recommendation = await ctx.agent.recommend(context)
            usages = recommendation.usages
            budget = record_usages(state, ctx.budget_config, usages)
            validate_review_response(context, recommendation, recommend=True)
            plan, records = apply_delegated_review(
                plan, recommendation.recommendations, actor=actor, delegated_at=card.delegated_at,
            )
        except BudgetExceeded:
            raise
        except Exception:
            # Provider errors may contain credentials; never expose their payloads.
            raise ValueError("Delegated review failed; retry or switch to manual review") from None
        if not plan.approval_ready(actor):
            raise ValueError("Delegated recommendations did not satisfy approval requirements")
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
                "changes_json": [record.model_dump(mode="json") for record in records],
                "approved_at": _now() if delegated else None,
            }
        )
        .execute()
        .data[0]
    )
    if delegated:
        ctx.admin.table("experiments").update({"status": "approved"}).eq(
            "id", str(experiment_id),
        ).execute()
    return {
        **updates,
        "experiment_id": str(experiment_id),
        "card": card,
        "plan": plan,
        "plan_id": plan_row["id"],
        "plan_version": 1,
        "plan_approved": delegated,
        "budget": budget,
        "usages": usages,
    }


async def human_approve_plan(state: dict, ctx: WorkflowContext) -> dict:
    from langgraph.types import interrupt

    updates = ctx.set_status(state, WorkflowStatus.AWAITING_PLAN_APPROVAL, "human_approve_plan")
    try:
        _, latest, plan, _, card, _ = load_execution_plan(
            ctx.admin, state["experiment_id"], project_id=state["project_id"],
            paper_id=state["paper_id"], spec_id=state["spec_version_id"],
        )
        delegated_ready = plan.review_mode is ReviewMode.DELEGATED
    except ValueError:
        delegated_ready = False
    if not delegated_ready:
        interrupt({"stage": "plan", "experiment_id": state.get("experiment_id")})
        # A queue resume is merely a wake-up signal, not approval authority.
        _, latest, plan, _, card, _ = load_execution_plan(
            ctx.admin, state["experiment_id"], project_id=state["project_id"],
            paper_id=state["paper_id"], spec_id=state["spec_version_id"],
        )
    return {
        **updates,
        "card": card,
        "plan": plan,
        "plan_id": latest["id"],
        "plan_version": latest["version"],
        "plan_approved": True,
    }


def _selected_execution(state: dict, ctx: WorkflowContext):
    selected = load_execution_plan(
        ctx.admin, state["experiment_id"], state["plan_id"],
        project_id=state["project_id"], paper_id=state["paper_id"],
        spec_id=state["spec_version_id"],
    )
    _, row, plan, spec, card, _ = selected
    if row["version"] != state["plan_version"] or spec["version"] != state["spec_version"]:
        raise ValueError("Selected execution versions do not match the checkpoint")
    if plan != state["plan"] or card != state["card"]:
        raise ValueError("Selected execution content changed since approval/code generation")
    if state.get("run_id"):
        runs = (
            ctx.admin.table("execution_runs").select("experiment_id")
            .eq("id", state["run_id"]).execute().data
        )
        if not runs or runs[0]["experiment_id"] != state["experiment_id"]:
            raise ValueError("Run does not belong to the selected experiment")
    return selected


async def generate_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.GENERATING_CODE, "generate_code")
    _, _, plan, _, card, _ = _selected_execution(state, ctx)
    response = await ctx.agent.codegen(CodegenRequest(card=card, plan=plan))
    bundle = response.bundle
    run_id = state.get("run_id")
    if not run_id:
        run_id = str(uuid4())
        ctx.admin.table("execution_runs").insert(
            {
                "id": run_id,
                "experiment_id": state["experiment_id"],
                "plan_id": state["plan_id"],
                "code_version": 0,
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
    ).eq("id", state["run_id"]).eq(
        "repair_attempt", state.get("repair_attempt", 0)
    ).neq("status", RunStatus.CANCELLED.value).neq(
        "status", RunStatus.SUCCEEDED.value
    ).execute()
    return {
        **updates,
        "validation_ok": False,
        "classification": classification,
        "error": error,
    }


async def execute_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.EXECUTING, "execute_code")
    run_id = state["run_id"]
    row = load_run_row(ctx.admin, run_id)
    if row is None:
        raise ValueError("Execution run is unavailable")
    if row["status"] in (RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value):
        return {**updates, "execution_result": terminal_result_from_row(row)}

    attempt = state.get("repair_attempt", 0)
    if row["status"] in (RunStatus.FAILED.value, RunStatus.TIMEOUT.value) and (
        row.get("repair_attempt", 0) >= attempt
    ):
        return {**updates, "execution_result": terminal_result_from_row(row)}
    if time_budget_exhausted(state, ctx.budget_config):
        error = "experiment time budget exhausted"
        _finalize_run(ctx.admin, run_id, RunStatus.FAILED.value, error, attempt=attempt)
        return {**updates, "budget_exhausted": True, "error": error}

    # A newer draft must stop dispatch, never replace the plan under generated code.
    _, _, plan, _, _, _ = _selected_execution(state, ctx)
    bundle = state["code_bundle"]
    reused = _reuse_code_version(ctx.admin, state, row, bundle, attempt)
    if reused is None:
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
            ctx.admin, state["experiment_id"], state["code_version"], bundle,
            change_reason, related_error,
        )
        pin_run_files(ctx.admin, run_id, file_ids)
    else:
        code_version, file_ids = reused
        pins = ctx.admin.table("execution_run_files").select("generated_file_id").eq(
            "run_id", run_id
        ).execute().data
        if not pins:
            pin_run_files(ctx.admin, run_id, file_ids)

    claimed = mark_attempt_running(
        ctx.admin, row, attempt=attempt, code_version=code_version, code_hash=bundle.code_hash,
        plan_id=state["plan_id"], plan_version=state["plan_version"],
    )
    row = load_run_row(ctx.admin, run_id)
    if claimed is None or row is None:
        if row is not None and row["status"] in (
            RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value, RunStatus.TIMEOUT.value,
        ):
            return {**updates, "execution_result": terminal_result_from_row(row)}
        raise RuntimeError("Execution attempt ownership was lost")

    request = build_run_request(
        UUID(state["experiment_id"]), UUID(state["plan_id"]), bundle, plan,
        code_version, state["plan_version"], UUID(run_id), attempt,
    )
    runner_run_id = UUID(row["runner_run_id"]) if row.get("runner_run_id") else None
    result = None
    try:
        if runner_run_id is None:
            # Re-check immediately before dispatch; the conditional Runner-id
            # write below closes the remaining acceptance/persistence window.
            current = load_run_row(ctx.admin, run_id)
            if current["status"] == RunStatus.CANCELLED.value:
                return {**updates, "execution_result": terminal_result_from_row(current)}
            accepted = await ctx.runner.start_run(request)
            runner_run_id = accepted.run_id
            if not persist_runner_run_id(ctx.admin, run_id, runner_run_id, attempt):
                current = load_run_row(ctx.admin, run_id)
                if current["status"] != RunStatus.SUCCEEDED.value:
                    await _cancel_runner(ctx.runner, runner_run_id)
                return {**updates, "execution_result": terminal_result_from_row(current)}

        result = await _poll_run(ctx.runner, runner_run_id)
        await async_persist_result(ctx.admin, run_id, runner_run_id, result, ctx.runner, attempt)
        _finalize_run(ctx.admin, run_id, result.status.value, result.error, attempt=attempt)
    except asyncio.CancelledError:
        await _cancel_runner(ctx.runner, runner_run_id or request.request_id)
        raise
    except Exception as exc:
        if runner_run_id is not None:
            await _cancel_runner(ctx.runner, runner_run_id)
        else:
            # A transport error can happen after Runner acceptance. Cancel the
            # deterministic identity even though no accepted id was received.
            await _cancel_runner(ctx.runner, request.request_id)
        current = load_run_row(ctx.admin, run_id)
        if current["status"] in (RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value):
            result = terminal_result_from_row(current)
        elif result is not None and result.status in TERMINAL_RUN:
            # The Runner's real terminal outcome remains authoritative even if
            # storing its audit data failed.
            _finalize_run(ctx.admin, run_id, result.status.value, result.error, attempt=attempt)
        else:
            result = RunResult(
                run_id=UUID(run_id), status=RunStatus.FAILED,
                error=f"Runner execution failed: {exc}", error_category=ErrorCategory.UNKNOWN_ERROR,
            )
            _finalize_run(ctx.admin, run_id, result.status.value, result.error, attempt=attempt)

    budget = state["budget"].model_copy(deep=True)
    budget.record_experiment_time(result.execution_time or 0.0)
    return {
        **updates,
        "execution_result": result,
        "runner_run_id": str(runner_run_id) if runner_run_id else None,
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
    ctx.admin.table("execution_runs").update(
        {"error_category": classification.category.value}
    ).eq("id", state["run_id"]).eq(
        "repair_attempt", state.get("repair_attempt", 0)
    ).neq("status", RunStatus.CANCELLED.value).neq(
        "status", RunStatus.SUCCEEDED.value
    ).execute()
    return {**updates, "classification": classification}


async def repair_code(state: dict, ctx: WorkflowContext) -> dict:
    updates = ctx.set_status(state, WorkflowStatus.REPAIRING, "repair_code")
    classification = state["classification"]
    result = state.get("execution_result")
    _selected_execution(state, ctx)
    if state["budget"].repair_count >= ctx.budget_config.max_repairs:
        raise BudgetExceeded("Repair budget exhausted")
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
        if classification.category is not ErrorCategory.OUT_OF_MEMORY:
            raise ValueError("Only bounded OOM scaling can replan automatically")
        if budget.replan_count >= ctx.budget_config.max_replans:
            raise BudgetExceeded("OOM replan budget exhausted")
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
            max_replans=ctx.budget_config.max_replans,
        )
        plan = ExperimentPlan.model_validate(new_row["config_json"])
        plan_id = new_row["id"]
        plan_version = new_row["version"]
        budget.replan_count += 1
        _, _, plan, _, card, _ = load_execution_plan(
            ctx.admin, state["experiment_id"], plan_id, project_id=state["project_id"],
            paper_id=state["paper_id"], spec_id=state["spec_version_id"],
        )
        bundle = (await ctx.agent.codegen(CodegenRequest(card=card, plan=plan))).bundle
    return {
        **updates,
        "code_bundle": bundle,
        "code_version": state["code_version"] + 1,
        "repair_attempt": state.get("repair_attempt", 0) + 1,
        "replan_count": budget.replan_count,
        "plan": plan,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "budget": budget,
    }


async def evaluate_result(state: dict, ctx: WorkflowContext) -> dict:
    from ..services.reports import build_evaluate_request

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
    result = state.get("execution_result")
    if result is not None and result.status is RunStatus.CANCELLED and not pipeline_failed:
        status = WorkflowStatus.CANCELLED
    else:
        status = WorkflowStatus.FAILED if pipeline_failed else WorkflowStatus.COMPLETED
    if state.get("validation_ok") is False and state.get("run_id"):
        run_row = load_run_row(ctx.admin, state["run_id"])
        _finalize_run(
            ctx.admin, state["run_id"], RunStatus.FAILED.value, state.get("error"),
            attempt=run_row.get("repair_attempt", 0) if run_row else 0,
        )
    elif result is not None and result.status is RunStatus.CANCELLED and state.get("run_id"):
        run_row = load_run_row(ctx.admin, state["run_id"])
        _finalize_run(
            ctx.admin, state["run_id"], RunStatus.CANCELLED.value, result.error,
            attempt=run_row.get("repair_attempt", 0) if run_row else 0,
        )
    # Execution reconciliation already mirrors the status to the experiment with
    # a latest-run guard. Do not overwrite a newer run here.
    ctx.finalize_workflow(state, status, state.get("error"))
    return {"status": status.value, "current_node": "persist_results"}
