"""Persistence helpers for the execution/repair loop (Worker, API trust domain).

Every code generation or repair inserts a NEW append-only generated_files version
with its sha256, change reason and related error; each run pins the exact files it
executed via execution_run_files. Logs and artifacts are stored in Supabase
storage; the run row records metrics, exit code and the full audit blob. OOM is
handled by inserting a new Planner-owned plan version (reduced batch), never by
silently editing training targets in code.
"""

from datetime import UTC, datetime
from uuid import UUID

from reproagent_schemas import (
    CodeBundle,
    ExperimentPlan,
    PlanChange,
    PlanEditRequest,
    PlanOrigin,
    ResourceBudget,
    RunRequest,
    RunResult,
)

from .plan_edits import apply_plan_edits
from .versions import insert_versioned

LOGS_BUCKET = "runs"


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


def build_run_request(
    experiment_id: UUID,
    plan_id: UUID,
    bundle: CodeBundle,
    plan: ExperimentPlan,
    code_version: int,
    plan_version: int,
) -> RunRequest:
    budget = ResourceBudget(max_seconds=min(plan.runtime_budget.max_seconds, 600))
    return RunRequest(
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
    _ensure_bucket(admin)
    prefix = f"{run_id}/attempt-{attempt}"
    stdout_path = f"{prefix}/stdout.txt"
    stderr_path = f"{prefix}/stderr.txt"
    _upload(admin, stdout_path, result.stdout.encode("utf-8"))
    _upload(admin, stderr_path, result.stderr.encode("utf-8"))

    admin.table("execution_artifacts").delete().eq("run_id", run_id).execute()
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
    admin.table("execution_runs").update(update).eq("id", run_id).execute()


def create_oom_plan(
    admin, experiment_id: str, base_row: dict, run_id: UUID, error: str
) -> dict:
    plan = ExperimentPlan.model_validate(base_row["config_json"])
    current_batch = plan.training_config.batch_size.value
    new_batch = max(1, current_batch // 2)
    edit = PlanEditRequest(
        base_version=base_row["version"],
        changes=[
            PlanChange(
                field_path="training_config.batch_size", value=new_batch, human_confirmed=True
            )
        ],
        change_reason=f"OOM retry: reduce batch size {current_batch} -> {new_batch}",
        origin=PlanOrigin.OOM_RETRY,
        related_error=error[:2000],
        related_run_id=run_id,
    )
    new_plan, records = apply_plan_edits(plan, edit)

    def next_version() -> int:
        latest = (
            admin.table("experiment_plans")
            .select("version")
            .eq("experiment_id", experiment_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return latest[0]["version"] + 1 if latest else 1

    def insert(v: int) -> dict:
        row = {
            "experiment_id": experiment_id,
            "version": v,
            "config_json": new_plan.model_dump(mode="json"),
            "budget_json": new_plan.runtime_budget.model_dump(mode="json"),
            "origin": PlanOrigin.OOM_RETRY.value,
            "parent_version_id": base_row["id"],
            "change_reason": edit.change_reason,
            "related_error": error[:2000],
            "related_run_id": str(run_id),
            "changes_json": [r.model_dump(mode="json") for r in records],
            "approved_at": datetime.now(UTC).isoformat(),
        }
        return admin.table("experiment_plans").insert(row).execute().data[0]

    return insert_versioned(next_version, insert)