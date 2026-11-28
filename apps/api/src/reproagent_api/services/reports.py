"""Build evaluations and append immutable reports from real persisted evidence.

When attempt history exists, only the greatest 1-based attempt is authoritative:
its Plan, generated-file pins, artifact rows, and metrics.json are used together.
The mutable Run projection is never allowed to mix an earlier attempt's evidence
into a later attempt's report. Pre-attempt legacy failures retain the old Run
projection and explicitly leave execution_attempt unset.
"""

import json
import time

from reproagent_schemas import (
    CodeFile,
    ErrorCategory,
    EvaluateRequest,
    ExperimentPlan,
    ReportReferences,
    ReportVersion,
    ReproductionCard,
    RunAudit,
    RunStatus,
    compute_code_hash,
)

from .review_audit import collect_review_decisions


class ReportError(Exception):
    code = "REPORT_UNAVAILABLE"


class ReportNotFound(ReportError):
    code = "REPORT_UNAVAILABLE"


class ReportVersionNotFound(ReportError):
    code = "REPORT_VERSION_NOT_FOUND"


def _workflow_id(admin, run_id: str, workflow_id: str | None) -> str | None:
    query = admin.table("workflows").select("id").eq("run_id", run_id)
    if workflow_id is not None:
        query = query.eq("id", workflow_id)
    rows = query.order("created_at", desc=True).limit(1).execute().data
    return rows[0]["id"] if rows else None


def _plan(admin, plan_id: str, experiment_id: str) -> dict:
    rows = (
        admin.table("experiment_plans")
        .select("*")
        .eq("id", plan_id)
        .eq("experiment_id", experiment_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise ValueError("Pinned experiment plan is unavailable")
    return rows[0]


def _file_ids(admin, table: str, run_id: str, attempt: int | None) -> list[str]:
    query = admin.table(table).select("generated_file_id").eq("run_id", run_id)
    if attempt is not None:
        query = query.eq("attempt", attempt)
    return sorted(row["generated_file_id"] for row in query.execute().data)


def _code_files(admin, table: str, run_id: str, attempt: int | None) -> list[CodeFile]:
    file_ids = _file_ids(admin, table, run_id, attempt)
    if not file_ids:
        return []
    rows = (
        admin.table("generated_files")
        .select("path,content,sha256")
        .in_("id", file_ids)
        .execute()
        .data
    )
    if len(rows) != len(set(file_ids)):
        raise ValueError("A pinned generated file is unavailable")
    return sorted(
        (
            CodeFile(path=row["path"], content=row["content"] or "", sha256=row["sha256"])
            for row in rows
        ),
        key=lambda file: (file.path, file.sha256, file.content),
    )


def _artifact_metrics(
    admin, run_id: str, attempt: int | None, artifacts: list[dict]
) -> dict:
    if attempt is None:
        # Only failures before attempt creation may use the legacy Run metrics.
        return {}
    metrics_rows = [row for row in artifacts if row["artifact_type"] == "metrics.json"]
    if not metrics_rows:
        return {}
    data = admin.storage.from_("runs").download(metrics_rows[0]["storage_path"])
    metrics = json.loads(data)
    if not isinstance(metrics, dict):
        raise ValueError("The final attempt's metrics artifact must be a JSON object")
    return metrics


def build_evaluate_request(
    admin, run_id: str, *, workflow_id: str | None = None
) -> EvaluateRequest:
    run = admin.table("execution_runs").select("*").eq("id", run_id).single().execute().data
    if run is None:
        raise ValueError("Execution run is unavailable")
    experiment = (
        admin.table("experiments")
        .select("*")
        .eq("id", run["experiment_id"])
        .single()
        .execute()
        .data
    )
    spec_row = (
        admin.table("reproduction_specs")
        .select("id,version,spec_json")
        .eq("id", experiment["spec_id"])
        .eq("paper_id", experiment["paper_id"])
        .single()
        .execute()
        .data
    )
    paper = (
        admin.table("papers")
        .select("title")
        .eq("id", experiment["paper_id"])
        .single()
        .execute()
        .data
    )

    attempts = (
        admin.table("execution_attempts")
        .select("*")
        .eq("run_id", run_id)
        .order("attempt", desc=True)
        .limit(1)
        .execute()
        .data
    )
    final_attempt = attempts[0] if attempts else None
    attempt_number = final_attempt["attempt"] if final_attempt else None
    if final_attempt is None:
        plan_row = _plan(admin, run["plan_id"], experiment["id"])
        code_files = _code_files(admin, "execution_run_files", run_id, None)
        artifact_rows = [
            row for row in admin.table("execution_artifacts")
            .select("artifact_type,storage_path")
            .eq("run_id", run_id).execute().data
            if row.get("attempt") is None
        ]
        metrics = run.get("metrics_json") or {}
        plan_version = plan_row["version"]
        code_version = run["code_version"]
        code_hash = run.get("code_hash")
        request_id = None
        runner_run_id = None
    else:
        plan_row = _plan(admin, final_attempt["plan_id"], experiment["id"])
        if (
            plan_row["version"] != final_attempt["plan_version"]
            or plan_row["id"] != final_attempt["plan_id"]
        ):
            raise ValueError("The final attempt's pinned Plan version is inconsistent")
        code_files = _code_files(admin, "execution_attempt_files", run_id, attempt_number)
        artifact_rows = (
            admin.table("execution_artifacts")
            .select("artifact_type,storage_path")
            .eq("run_id", run_id)
            .eq("attempt", attempt_number)
            .execute()
            .data
        )
        artifact_rows.sort(key=lambda row: (row["artifact_type"], row["storage_path"]))
        metrics = _artifact_metrics(admin, run_id, attempt_number, artifact_rows)
        plan_version = final_attempt["plan_version"]
        code_version = final_attempt["code_version"]
        code_hash = compute_code_hash(code_files) if code_files else None
        request_id = final_attempt.get("runner_request_id")
        runner_run_id = final_attempt.get("runner_run_id")

    review_decisions = collect_review_decisions(
        admin,
        paper_id=str(experiment["paper_id"]),
        experiment_id=str(experiment["id"]),
        spec_id=str(spec_row["id"]),
        plan_id=str(plan_row["id"]),
    )
    references = ReportReferences(
        paper_id=experiment["paper_id"],
        spec_version_id=spec_row["id"],
        spec_version=spec_row["version"],
        experiment_id=experiment["id"],
        plan_id=plan_row["id"],
        plan_version=plan_version,
        code_version=code_version,
        code_hash=code_hash,
        run_id=run_id,
        workflow_id=_workflow_id(admin, run_id, workflow_id),
        execution_attempt=attempt_number,
        execution_request_id=request_id,
        runner_run_id=runner_run_id,
        generated_file_ids=_file_ids(
            admin,
            "execution_attempt_files" if final_attempt else "execution_run_files",
            run_id,
            attempt_number,
        ),
        review_trace_ids=[decision.id for decision in review_decisions],
    )
    audit = RunAudit.model_validate(run["audit_json"]) if run.get("audit_json") else None
    return EvaluateRequest(
        references=references,
        review_decisions=review_decisions,
        card=ReproductionCard.model_validate(spec_row["spec_json"]),
        plan=ExperimentPlan.model_validate(plan_row["config_json"]),
        paper_title=paper["title"],
        code_files=code_files,
        run_status=RunStatus(run["status"]),
        metrics=metrics,
        audit=audit,
        execution_time=(run.get("runtime_json") or {}).get("execution_time"),
        exit_code=run.get("exit_code"),
        error=run.get("error"),
        error_category=ErrorCategory(run["error_category"]) if run.get("error_category") else None,
        stdout_path=run.get("stdout_path"),
        stderr_path=run.get("stderr_path"),
        artifacts=sorted(row["artifact_type"] for row in artifact_rows),
    )


def append_report_version(
    admin,
    run_id: str,
    report_json: dict,
    markdown: str,
    generation_ms: float | None = None,
) -> ReportVersion:
    result = admin.rpc("append_report_version", {
        "p_run_id": run_id,
        "p_report_json": report_json,
        "p_markdown": markdown,
        "p_generation_ms": generation_ms,
    }).execute().data
    metadata = result[0] if isinstance(result, list) else result
    if not metadata:
        raise RuntimeError("Report append returned no version")
    rows = (
        admin.table("reproduction_reports")
        .select("*")
        .eq("run_id", run_id)
        .eq("version", metadata["version"])
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise RuntimeError("Appended report version is unavailable")
    return ReportVersion.model_validate(rows[0])


def latest_report_generation_seconds(admin, run_id: str, attempt: int) -> float | None:
    """Read evaluator latency from the latest immutable version for one attempt."""
    rows = (
        admin.table("reproduction_reports")
        .select("generation_ms")
        .eq("run_id", run_id)
        .eq("attempt", attempt)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows or rows[0].get("generation_ms") is None:
        return None
    return rows[0]["generation_ms"] / 1000


def get_report(admin, run_id: str, version: int | None = None) -> ReportVersion:
    if version is not None and version <= 0:
        raise ValueError("Report version must be a positive integer")
    query = admin.table("reproduction_reports").select("*").eq("run_id", run_id)
    if version is None:
        query = query.order("version", desc=True).limit(1)
    else:
        query = query.eq("version", version).limit(1)
    rows = query.execute().data
    if rows:
        return ReportVersion.model_validate(rows[0])
    if version is not None:
        history = (
            admin.table("reproduction_reports")
            .select("version").eq("run_id", run_id).limit(1).execute().data
        )
        if history:
            raise ReportVersionNotFound(f"Report version {version} does not exist")
    raise ReportNotFound("Report has not been generated")


async def generate_and_store_report(
    admin, agent, run_id: str, *, workflow_id: str | None = None
) -> ReportVersion:
    request = build_evaluate_request(admin, run_id, workflow_id=workflow_id)
    started = time.perf_counter()
    response = await agent.evaluate(request)
    generation_ms = (time.perf_counter() - started) * 1000
    return append_report_version(
        admin,
        run_id,
        response.report.model_dump(mode="json"),
        response.markdown,
        generation_ms,
    )
