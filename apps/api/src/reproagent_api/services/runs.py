"""Persistence helpers for the execution/repair loop (Worker, API trust domain).

Every code generation or repair inserts a NEW append-only generated_files version
with its sha256, change reason and related error; each run pins the exact files it
executed via execution_run_files. Logs and artifacts are stored in Supabase
storage; the run row records metrics, exit code and the full audit blob. OOM is
handled by inserting a new Planner-owned plan version (reduced batch), never by
silently editing training targets in code.
"""

import hashlib
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    BudgetConfig,
    CodeBundle,
    ConfirmationSource,
    ExperimentPlan,
    ParamSource,
    PlanChangeRecord,
    PlanOrigin,
    ReproductionCard,
    ResourceBudget,
    ReviewMode,
    RiskTier,
    RunAudit,
    RunRequest,
    RunResult,
    RunStatus,
)

from .versions import insert_versioned

LOGS_BUCKET = "runs"


def project_actor(admin, project_id: str) -> UUID:
    """Resolve authority from the database, never workflow/queue-supplied user data."""
    rows = admin.table("projects").select("user_id").eq("id", project_id).execute().data
    if not rows or not rows[0].get("user_id"):
        raise ValueError("Project owner is unavailable")
    return UUID(rows[0]["user_id"])


def load_confirmed_card(
    admin, project_id: str, paper_id: str, spec_id: str | None = None,
) -> tuple[dict, ReproductionCard, UUID]:
    actor = project_actor(admin, project_id)
    papers = (
        admin.table("papers").select("id").eq("id", paper_id)
        .eq("project_id", project_id).execute().data
    )
    if not papers:
        raise ValueError("Paper does not belong to the project")
    query = admin.table("reproduction_specs").select("*").eq("paper_id", paper_id)
    if spec_id is not None:
        query = query.eq("id", spec_id)
    rows = query.order("version", desc=True).limit(1).execute().data
    if not rows or rows[0].get("status") != "confirmed":
        raise ValueError("Latest selected Card must be confirmed before planning or execution")
    row = rows[0]
    card = ReproductionCard.model_validate(row["spec_json"])
    if card.review_mode is ReviewMode.DELEGATED and card.delegated_by != actor:
        raise ValueError("Card delegation does not belong to the project owner")
    return row, card, actor


def load_execution_plan(
    admin, experiment_id: str, plan_id: str | None = None, *,
    project_id: str | None = None, paper_id: str | None = None, spec_id: str | None = None,
) -> tuple[dict, dict, ExperimentPlan, dict, ReproductionCard, UUID]:
    """Verify ownership and the latest approved plan without replacing an execution pin."""
    rows = admin.table("experiments").select("*").eq("id", experiment_id).execute().data
    if not rows:
        raise ValueError("Experiment is unavailable")
    experiment = rows[0]
    for key, expected in (("project_id", project_id), ("paper_id", paper_id), ("spec_id", spec_id)):
        if expected is not None and experiment.get(key) != expected:
            raise ValueError("Experiment does not match the selected workflow context")
    if not experiment.get("spec_id"):
        raise ValueError("Experiment has no confirmed Card")
    spec, card, actor = load_confirmed_card(
        admin, experiment["project_id"], experiment["paper_id"], experiment["spec_id"],
    )
    plans = (
        admin.table("experiment_plans").select("*").eq("experiment_id", experiment_id)
        .order("version", desc=True).limit(1).execute().data
    )
    if not plans or (plan_id is not None and plans[0]["id"] != plan_id):
        raise ValueError("Selected plan is not the latest experiment plan")
    row = plans[0]
    if not row.get("approved_at"):
        raise ValueError("Latest experiment plan has not been approved")
    plan = ExperimentPlan.model_validate(row["config_json"])
    if not plan.approval_ready(actor):
        raise ValueError("Plan approval or delegated authorization is invalid")
    return experiment, row, plan, spec, card, actor


def insert_code_version(
    admin,
    experiment_id: str,
    version: int,
    bundle: CodeBundle,
    change_reason: str,
    related_error: str | None = None,
) -> tuple[int, list[str]]:
    # generated_files is append-only per experiment: bump past any existing
    # version so repeated runs (and crash re-drives) never collide on
    # (experiment, version, path), while repair numbering stays monotonic
    def next_version() -> int:
        latest = (
            admin.table("generated_files")
            .select("version")
            .eq("experiment_id", experiment_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return max(version, latest[0]["version"] + 1) if latest else version

    def insert(v: int) -> tuple[int, list[str]]:
        rows = [
            {
                "experiment_id": experiment_id,
                "version": v,
                "path": f.path,
                "content": f.content,
                "sha256": f.sha256,
                "change_reason": change_reason,
                "related_error": related_error,
            }
            for f in bundle.files
        ]
        inserted = admin.table("generated_files").insert(rows).execute().data
        return v, [row["id"] for row in inserted]

    return insert_versioned(next_version, insert)


def pin_run_files(admin, run_id: str, file_ids: list[str]) -> None:
    admin.table("execution_run_files").delete().eq("run_id", run_id).execute()
    admin.table("execution_run_files").insert(
        [{"run_id": run_id, "generated_file_id": fid} for fid in file_ids]
    ).execute()


def run_creation_fingerprint(experiment_id: UUID, plan_id: UUID, plan_version: int) -> str:
    """Deterministic identity for a run-creation intent; plan config is immutable per version."""
    raw = f"{experiment_id}:{plan_id}:{plan_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def execution_request_id(run_id: UUID, repair_attempt: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"reproagent:execution:{run_id}:attempt:{repair_attempt}")


def build_run_request(
    experiment_id: UUID,
    plan_id: UUID,
    bundle: CodeBundle,
    plan: ExperimentPlan,
    code_version: int,
    plan_version: int,
    run_id: UUID,
    repair_attempt: int,
) -> RunRequest:
    budget = ResourceBudget(max_seconds=min(plan.runtime_budget.max_seconds, 600))
    return RunRequest(
        request_id=execution_request_id(run_id, repair_attempt),
        experiment_id=experiment_id,
        experiment_plan_id=plan_id,
        code_files=bundle.files,
        dependencies=bundle.requirements,
        resource_budget=budget,
        entrypoint=bundle.entrypoint,
        seed=plan.runtime_budget.seed,
        code_version=code_version,
        plan_version=plan_version,
        metrics_filename="metrics.json",
    )


def load_run_row(admin, run_id: str) -> dict | None:
    rows = admin.table("execution_runs").select("*").eq("id", run_id).execute().data
    return rows[0] if rows else None


def mark_attempt_running(
    admin,
    run_row: dict,
    *,
    attempt: int,
    code_version: int,
    code_hash: str,
    plan_id: str,
    plan_version: int,
    request_json: dict | None = None,
) -> dict | None:
    """Atomically open one attempt without reviving protected terminal states.

    Recovery may continue the same running attempt. Only a strictly newer repair
    attempt may deliberately reopen a failed/timeout row.
    """
    previous_attempt = run_row.get("repair_attempt", 0)
    status = run_row.get("status")
    if status in (RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value):
        return None
    if status in (RunStatus.FAILED.value, RunStatus.TIMEOUT.value):
        if attempt != previous_attempt + 1:
            return None
        allowed = [RunStatus.FAILED.value, RunStatus.TIMEOUT.value]
    else:
        if attempt == previous_attempt:
            allowed = [RunStatus.QUEUED.value, RunStatus.DISPATCHING.value, RunStatus.RUNNING.value]
        elif (
            attempt == previous_attempt + 1
            and status in (RunStatus.QUEUED.value, RunStatus.DISPATCHING.value)
            and not run_row.get("runner_run_id")
            and not run_row.get("started_at")
            and not run_row.get("finished_at")
            and int(run_row.get("code_version") or 0) <= 0
        ):
            # Static validation can repair before the first Runner dispatch. A
            # positive code version or any execution marker means an actual
            # attempt already owned this row and must not be skipped/stolen.
            allowed = [RunStatus.QUEUED.value, RunStatus.DISPATCHING.value]
        else:
            return None
    now = datetime.now(UTC).isoformat()
    payload = {
        "status": RunStatus.RUNNING.value,
        "repair_attempt": attempt,
        "code_version": code_version,
        "code_hash": code_hash,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "started_at": run_row.get("started_at") or now,
        "updated_at": now,
    }
    if request_json is not None:
        payload["request_json"] = request_json
    if status in (RunStatus.FAILED.value, RunStatus.TIMEOUT.value):
        # A deliberate repair is a new Runner request identity, not a reattach
        # to the preceding attempt's already-terminal container.
        payload["runner_run_id"] = None
        payload["started_at"] = now
        payload["finished_at"] = None
    rows = (
        admin.table("execution_runs")
        .update(payload)
        .eq("id", run_row["id"])
        .eq("repair_attempt", previous_attempt)
        .in_("status", allowed)
        .execute()
        .data
    )
    return rows[0] if rows else None


def persist_runner_run_id(admin, run_id: str, runner_run_id: UUID, attempt: int) -> bool:
    rows = (
        admin.table("execution_runs")
        .update({"runner_run_id": str(runner_run_id), "updated_at": datetime.now(UTC).isoformat()})
        .eq("id", run_id)
        .eq("repair_attempt", attempt)
        .neq("status", RunStatus.CANCELLED.value)
        .neq("status", RunStatus.SUCCEEDED.value)
        .execute()
        .data
    )
    return bool(rows)


def finalize_execution_run(
    admin,
    run_id: str,
    status: RunStatus,
    error: str | None,
    *,
    attempt: int | None = None,
    require_latest: bool = False,
) -> bool:
    params = {
        "p_run_id": run_id,
        "p_status": status.value,
        "p_error": redact_error(error),
    }
    if attempt is not None:
        params["p_repair_attempt"] = attempt
        params["p_require_latest"] = require_latest
    return bool(admin.rpc("reconcile_execution_run", params))


def redact_error(error: str | None) -> str | None:
    if error is None:
        return None
    from ..workflow.harness import redact

    return redact(error)


def terminal_result_from_row(row: dict) -> RunResult:
    status = RunStatus(row["status"])
    runtime = row.get("runtime_json") or {}
    return RunResult(
        run_id=UUID(row["id"]),
        status=status,
        exit_code=row.get("exit_code"),
        metrics=row.get("metrics_json") or {},
        execution_time=runtime.get("execution_time"),
        error=row.get("error"),
        error_category=row.get("error_category"),
        audit=RunAudit.model_validate(row["audit_json"]) if row.get("audit_json") else None,
    )


def _ensure_bucket(admin) -> None:
    try:
        admin.storage.create_bucket(LOGS_BUCKET, options={"public": False})
    except Exception:
        pass


def _upload(admin, path: str, data: bytes) -> None:
    admin.storage.from_(LOGS_BUCKET).upload(
        path, data, {"content-type": "application/octet-stream", "upsert": "true"}
    )


async def async_persist_result(
    admin, run_id: str, runner_run_id: UUID, result: RunResult, runner, attempt: int
) -> None:
    # Claim the exact nonterminal attempt before touching storage or artifact
    # rows. A stale attempt, cancellation, or concurrent success must not delete
    # the current attempt's protected output.
    claimed = (
        admin.table("execution_runs")
        .update({"updated_at": datetime.now(UTC).isoformat()})
        .eq("id", run_id)
        .eq("repair_attempt", attempt)
        .in_(
            "status",
            [RunStatus.QUEUED.value, RunStatus.DISPATCHING.value, RunStatus.RUNNING.value],
        )
        .execute()
        .data
    )
    if not claimed or claimed[0].get("repair_attempt", attempt) != attempt:
        return

    _ensure_bucket(admin)
    prefix = f"{run_id}/attempt-{attempt}"
    stdout_path = f"{prefix}/stdout.txt"
    stderr_path = f"{prefix}/stderr.txt"
    _upload(admin, stdout_path, result.stdout.encode("utf-8"))
    _upload(admin, stderr_path, result.stderr.encode("utf-8"))

    existing = (
        admin.table("execution_artifacts")
        .select("id,metadata_json,storage_path")
        .eq("run_id", run_id)
        .execute()
        .data
    )
    artifact_prefix = f"{prefix}/artifacts/"
    replace_ids = [
        row["id"]
        for row in existing
        if str((row.get("metadata_json") or {}).get("attempt")) == str(attempt)
        or (
            (row.get("metadata_json") or {}).get("attempt") is None
            and str(row.get("storage_path") or "").startswith(artifact_prefix)
        )
    ]
    if replace_ids:
        admin.table("execution_artifacts").delete().in_("id", replace_ids).execute()
    artifact_rows = []
    for name in result.artifacts:
        try:
            data = await runner.get_artifact_bytes(runner_run_id, name)
        except Exception:
            continue
        storage_path = f"{prefix}/artifacts/{name}"
        _upload(admin, storage_path, data)
        artifact_rows.append(
            {
                "run_id": run_id,
                "artifact_type": name,
                "storage_path": storage_path,
                "metadata_json": {
                    "attempt": attempt,
                    "code_hash": result.audit.code_hash if result.audit else None,
                },
            }
        )
    if artifact_rows:
        admin.table("execution_artifacts").insert(artifact_rows).execute()

    update = {
        "exit_code": result.exit_code,
        "metrics_json": result.metrics,
        "runtime_json": {"execution_time": result.execution_time},
        "audit_json": result.audit.model_dump(mode="json") if result.audit else {},
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
    }
    # Preserve the last detected error category across a successful repair retry;
    # only overwrite when this result actually carries an error.
    if result.error_category is not None:
        update["error_category"] = result.error_category.value
    if result.error is not None:
        update["error"] = result.error
    (
        admin.table("execution_runs")
        .update(update)
        .eq("id", run_id)
        .eq("repair_attempt", attempt)
        .neq("status", RunStatus.CANCELLED.value)
        .neq("status", RunStatus.SUCCEEDED.value)
        .execute()
    )


def create_oom_plan(
    admin, experiment_id: str, base_row: dict, run_id: UUID, error: str, *,
    max_replans: int | None = None,
) -> dict:
    # Lazy import avoids the workflow package's graph/services import cycle.
    from ..workflow.harness import redact

    _, base_row, plan, _, _, actor = load_execution_plan(admin, experiment_id, base_row["id"])
    runs = (
        admin.table("execution_runs").select("id,plan_id").eq("id", str(run_id))
        .eq("experiment_id", experiment_id).execute().data
    )
    if not runs or runs[0]["plan_id"] != base_row["id"]:
        raise ValueError("OOM repair must use the run's selected plan")
    previous = (
        admin.table("experiment_plans").select("id").eq("experiment_id", experiment_id)
        .eq("related_run_id", str(run_id)).eq("origin", PlanOrigin.OOM_RETRY.value).execute().data
    )
    ceiling = BudgetConfig().max_replans
    if max_replans is not None:
        ceiling = min(ceiling, max_replans)
    if len(previous) >= ceiling:
        raise ValueError("OOM replan budget exhausted")
    current_batch = plan.training_config.batch_size.value
    if current_batch <= 1:
        raise ValueError("OOM batch size cannot be reduced further")
    new_batch = max(1, current_batch // 2)
    reason = f"System OOM scaling: reduce batch size {current_batch} -> {new_batch}"
    delegated = plan.review_mode is ReviewMode.DELEGATED
    provenance = ConfirmationSource.AUTO if delegated else None
    data = plan.model_dump(mode="json")
    data["training_config"]["batch_size"].update(
        value=new_batch, human_confirmed=False, confirmed_by=provenance,
        source=ParamSource.INFERRED if delegated else ParamSource.MVP_SCALING,
        rationale=reason, requires_confirmation=delegated,
    )
    path = "training_config.batch_size"
    prior = next((a for a in plan.assumptions if a.field_path == path), None)
    # Ordinary system scaling cannot replace a human decision on a critical assumption.
    if not delegated and prior is not None and prior.risk_tier is RiskTier.CRITICAL:
        raise ValueError("Critical batch size changes require a new human approval")
    data["assumptions"] = [a for a in data["assumptions"] if a["field_path"] != path]
    data["assumptions"].append(Assumption(
        field_path=path, kind=AssumptionKind.MVP_SCALING, value=new_batch, rationale=reason,
        requires_confirmation=delegated, human_confirmed=False, confirmed_by=provenance,
        risk_tier=prior.risk_tier if prior else RiskTier.ROUTINE,
    ).model_dump(mode="json"))
    new_plan = ExperimentPlan.model_validate(data)
    if not new_plan.approval_ready(actor):
        raise ValueError("OOM scaling cannot bypass remaining approval requirements")
    records = [PlanChangeRecord(
        field_path=path, before=current_batch, after=new_batch, confirmed_by=provenance,
        review_mode=plan.review_mode, actor=None, recommendation_reasoning=reason,
        recommendation_risk="Bounded batch reduction only; other parameters and scope unchanged",
    )]
    if delegated:
        records.insert(0, PlanChangeRecord(
            field_path="delegated_authorization",
            before={"delegated_by": str(plan.delegated_by),
                    "delegated_at": plan.delegated_at.isoformat()},
            after={"delegated_by": str(new_plan.delegated_by),
                   "delegated_at": new_plan.delegated_at.isoformat()},
            review_mode=plan.review_mode, actor=None,
            recommendation_reasoning="Inherited existing authorization for bounded OOM scaling",
        ))
    safe_error = redact(error)

    def next_version() -> int:
        # A concurrent new version must invalidate this repair's approval.
        load_execution_plan(admin, experiment_id, base_row["id"])
        return base_row["version"] + 1

    def insert(v: int) -> dict:
        row = {
            "experiment_id": experiment_id,
            "version": v,
            "config_json": new_plan.model_dump(mode="json"),
            "budget_json": new_plan.runtime_budget.model_dump(mode="json"),
            "origin": PlanOrigin.OOM_RETRY.value,
            "parent_version_id": base_row["id"],
            "change_reason": reason,
            "related_error": safe_error,
            "related_run_id": str(run_id),
            "changes_json": [r.model_dump(mode="json") for r in records],
            "approved_at": datetime.now(UTC).isoformat(),
        }
        return admin.table("experiment_plans").insert(row).execute().data[0]

    return insert_versioned(next_version, insert)