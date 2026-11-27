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

from postgrest.exceptions import APIError
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


def pin_run_files(admin, run_id: str, file_ids: list[str], *, attempt: int | None = None) -> None:
    """Project the latest attempt's file bindings and append to attempt history.

    ``execution_run_files`` stays a final-attempt projection. The authoritative
    history lives in ``execution_attempts`` + ``execution_attempt_files``; a new
    attempt never erases a previous attempt's bindings.

    ``attempt`` is 1-based. Callers still holding the 0-based ``repair_attempt``
    should pass ``attempt = repair_attempt + 1``; when omitted, the attempt is
    derived from the run row's ``repair_attempt`` (also mapped 1-based).
    """
    if attempt is None:
        rows = (
            admin.table("execution_runs").select("repair_attempt")
            .eq("id", run_id).execute().data
        )
        attempt = (rows[0].get("repair_attempt", 0) + 1) if rows else 1
    if attempt < 1:
        raise ValueError(f"attempt must be 1-based, got {attempt}")
    # Append the immutable history FIRST so an error inside the projection
    # cannot lose evidence that these files were bound. The projection itself is
    # deleted and reinserted by one database transaction.
    pin_attempt_files(admin, run_id, attempt, file_ids)
    admin.rpc("replace_run_projection", {
        "p_run_id": run_id,
        "p_attempt": attempt,
        "p_file_ids": file_ids,
    }).execute()


def _attempt_row_from_run(run_row: dict, attempt: int, *, status: str | None = None) -> dict:
    # The history ordinal is 1-based, but Runner idempotency remains 0-based.
    runner_request = execution_request_id(UUID(run_row["id"]), attempt - 1)
    current_attempt = int(run_row.get("repair_attempt") or 0) + 1
    inherited_status = run_row.get("status") if attempt == current_attempt else None
    return {
        "run_id": run_row["id"],
        "attempt": attempt,
        "plan_id": run_row.get("plan_id"),
        "plan_version": run_row.get("plan_version") or 1,
        "code_version": run_row.get("code_version") or 0,
        "status": status or inherited_status or RunStatus.QUEUED.value,
        "runner_request_id": str(runner_request),
        "runner_run_id": run_row.get("runner_run_id") if attempt == current_attempt else None,
    }


def pin_attempt_files(admin, run_id: str, attempt: int, file_ids: list[str]) -> None:
    """Atomically seal one exact file set through the database RPC.

    The RPC creates or locks the attempt, validates the run's code context, and
    seals the set (including ``[]``). Repeating an identical set is a no-op;
    changing it raises a conflict. No read/check/write sequence occurs in Python.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be 1-based, got {attempt}")
    try:
        admin.rpc("pin_attempt_files", {
            "p_run_id": run_id,
            "p_attempt": attempt,
            "p_file_ids": file_ids,
        }).execute()
    except APIError as exc:
        if exc.message == "ATTEMPT_FILES_CONFLICT":
            raise ValueError(
                f"Attempt {attempt} already pinned a different file set; "
                "create a new attempt instead of changing history"
            ) from exc
        if exc.message == "ATTEMPT_FILE_CONTEXT_MISMATCH":
            raise ValueError(
                "generated file does not exist or belongs to another experiment"
            ) from exc
        if exc.message == "ATTEMPT_PLAN_CONTEXT_MISMATCH":
            raise ValueError("attempt plan belongs to another experiment") from exc
        if exc.message == "ATTEMPT_FILES_DUPLICATE":
            raise ValueError("attempt file ids must not contain duplicates") from exc
        if exc.message == "ATTEMPT_RUN_NOT_FOUND":
            raise ValueError("Attempt pinning requires an existing execution run") from exc
        raise


def ensure_attempt_row(admin, run_row: dict, attempt: int, *, status: str | None = None) -> dict:
    """Idempotently insert an ``execution_attempts`` row derived from the run row.

    Existing rows are preserved: history is append-only. The returned dict is the
    authoritative row after the operation. Callers that need to *update* a
    non-terminal attempt's status (e.g. mark it running) should do so explicitly
    so that terminal states are never silently overwritten.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be 1-based, got {attempt}")
    existing = (
        admin.table("execution_attempts")
        .select("*")
        .eq("run_id", run_row["id"])
        .eq("attempt", attempt)
        .execute()
        .data
    )
    if existing:
        return existing[0]
    payload = _attempt_row_from_run(run_row, attempt, status=status)
    inserted = admin.table("execution_attempts").upsert(
        payload, on_conflict="run_id,attempt", ignore_duplicates=True,
    ).execute().data
    if inserted:
        return inserted[0]
    rows = (
        admin.table("execution_attempts")
        .select("*")
        .eq("run_id", run_row["id"])
        .eq("attempt", attempt)
        .execute()
        .data
    )
    return rows[0] if rows else payload


def update_attempt_status(
    admin, run_id: str, attempt: int, status: str, *,
    error: str | None = None, runner_request_id: str | None = None,
    runner_run_id: str | None = None, finished_at: str | None = None,
) -> None:
    """Advance one attempt row without touching terminal states.

    Protected (``succeeded``/``cancelled``) attempts never regress. Terminal
    writes always stamp ``finished_at`` exactly once.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be 1-based, got {attempt}")
    patch = {"status": status, "updated_at": datetime.now(UTC).isoformat()}
    if error is not None:
        patch["error_message"] = error
    if runner_request_id is not None:
        patch["runner_request_id"] = runner_request_id
    if runner_run_id is not None:
        patch["runner_run_id"] = runner_run_id
    if finished_at is not None:
        patch["finished_at"] = finished_at
    admin.table("execution_attempts").update(patch).eq("run_id", run_id).eq("attempt", attempt).neq(
        "status", RunStatus.SUCCEEDED.value
    ).neq("status", RunStatus.CANCELLED.value).execute()


def list_attempts(admin, run_id: str) -> list[dict]:
    """Return the append-only attempt history for a run with pinned files.

    Ordered ascending by ``attempt`` (1-based). Each entry contains ``files`` --
    the exact set of generated-file bindings pinned to that attempt. This is the
    authoritative record: ``execution_run_files`` is only the latest projection.
    """
    attempts = (
        admin.table("execution_attempts")
        .select("*")
        .eq("run_id", run_id)
        .order("attempt")
        .execute()
        .data
    )
    if not attempts:
        return []
    bindings = (
        admin.table("execution_attempt_files")
        .select("attempt,generated_file_id")
        .eq("run_id", run_id)
        .execute()
        .data
    )
    by_attempt: dict[int, list[dict]] = {}
    for row in bindings:
        by_attempt.setdefault(row["attempt"], []).append(
            {"generated_file_id": row["generated_file_id"]}
        )
    for entry in attempts:
        entry["files"] = by_attempt.get(entry["attempt"], [])
    return attempts


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

    ``attempt`` here is the 0-based ``repair_attempt`` on ``execution_runs``. The
    1-based ``execution_attempts.attempt`` ordinal used for the history table is
    derived at the boundary: ``history_attempt = attempt + 1``.
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
    if not rows:
        return None
    # Append the attempt row (1-based) so the history records the same moment the
    # projection transitioned to running. Existing history is never overwritten.
    history_attempt = attempt + 1
    updated_run = rows[0]
    # Pin the plan/code identity at creation; opening the attempt only advances
    # lifecycle fields, which the database append-only guard permits.
    attempt_source = {
        **updated_run,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "code_version": code_version,
    }
    ensure_attempt_row(admin, attempt_source, history_attempt, status=RunStatus.RUNNING.value)
    # The ensure helper only inserts if the row is absent; for a re-drive of the
    # same attempt (recovery) the row already exists in a non-terminal state and
    # must be advanced to running alongside the projection. A queued pin may
    # precede code/plan projection updates, so opening also binds those values.
    admin.table("execution_attempts").update(
        {"status": RunStatus.RUNNING.value, "code_version": code_version,
         "plan_id": plan_id, "plan_version": plan_version,
         "dispatch_started_at": updated_run.get("started_at") or now}
    ).eq("run_id", updated_run["id"]).eq("attempt", history_attempt).neq(
        "status", RunStatus.SUCCEEDED.value
    ).neq("status", RunStatus.CANCELLED.value).execute()
    return updated_run


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
    if not rows:
        return False
    # Scope identity to the owning 0-based repair attempt. A later repair resets
    # only the projection and must never erase the previous attempt's Runner id.
    update_attempt_status(
        admin,
        run_id,
        attempt + 1,
        RunStatus.RUNNING.value,
        runner_request_id=str(execution_request_id(UUID(run_id), attempt)),
        runner_run_id=str(runner_run_id),
    )
    return True


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
    finalized = bool(admin.rpc("reconcile_execution_run", params).execute().data)
    if finalized:
        # Mirror the terminal status onto the authoritative attempt history so a
        # later repair/dispatch never reopens a closed attempt row.
        history_attempt = (attempt + 1) if attempt is not None else None
        if history_attempt is not None:
            now = datetime.now(UTC).isoformat()
            patch = {"status": status.value, "finished_at": now}
            if error is not None:
                patch["error_message"] = redact_error(error)
            (
                admin.table("execution_attempts")
                .update(patch)
                .eq("run_id", run_id)
                .eq("attempt", history_attempt)
                .neq("status", RunStatus.SUCCEEDED.value)
                .neq("status", RunStatus.CANCELLED.value)
                .execute()
            )
    return finalized


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
    """Persist one Runner result against its owning attempt.

    ``attempt`` here is the 0-based ``repair_attempt`` still stored on
    ``execution_runs``. The 1-based history ordinal (``repair_attempt + 1``) is
    what lands in ``execution_artifacts.attempt`` and the storage path, so the
    per-attempt history matches ``execution_attempts.attempt``. A stale attempt,
    cancellation, or concurrent success must not delete the current attempt's
    protected output.
    """
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

    # 1-based attempt ordinal: {run_id}/attempt-<repair_attempt+1>/...
    history_attempt = attempt + 1

    _ensure_bucket(admin)
    prefix = f"{run_id}/attempt-{history_attempt}"
    stdout_path = f"{prefix}/stdout.txt"
    stderr_path = f"{prefix}/stderr.txt"
    _upload(admin, stdout_path, result.stdout.encode("utf-8"))
    _upload(admin, stderr_path, result.stderr.encode("utf-8"))

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
                "artifact_type": name,
                "storage_path": storage_path,
                "metadata_json": {
                    "attempt": history_attempt,
                    "code_hash": result.audit.code_hash if result.audit else None,
                },
            }
        )

    # No destructive database work happened before this call. The RPC takes the
    # Run lock, rechecks the owning nonterminal attempt, replaces only that
    # attempt's artifact rows, and applies the audit update atomically. A cancel
    # or terminal seal during storage/Runner I/O therefore wins without losing
    # existing evidence or overwriting terminal audit/error/path truth.
    admin.rpc(
        "persist_execution_result",
        {
            "p_run_id": run_id,
            "p_repair_attempt": attempt,
            "p_artifact_rows": artifact_rows,
            "p_exit_code": result.exit_code,
            "p_metrics_json": result.metrics,
            "p_execution_time": result.execution_time,
            "p_audit_json": result.audit.model_dump(mode="json") if result.audit else {},
            "p_stdout_path": stdout_path,
            "p_stderr_path": stderr_path,
            "p_error_category": (
                result.error_category.value if result.error_category is not None else None
            ),
            "p_error": result.error,
        },
    ).execute()


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