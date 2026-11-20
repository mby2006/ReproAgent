"""Report generation: build the evaluate request from REAL persisted run data.

Every experiment value is read from the run row (metrics_json/audit_json/
runtime_json), every paper value from the pinned reproduction_specs version,
and the implementation side from the exact generated_files bound to the run.
Nothing is inferred or fabricated here.
"""

from reproagent_schemas import (
    CodeFile,
    ErrorCategory,
    EvaluateRequest,
    ExperimentPlan,
    ReportReferences,
    ReproductionCard,
    RunAudit,
    RunStatus,
)

from .review_audit import collect_review_decisions


def build_evaluate_request(admin, run_id: str) -> EvaluateRequest:
    run = admin.table("execution_runs").select("*").eq("id", run_id).single().execute().data
    experiment = (
        admin.table("experiments")
        .select("*")
        .eq("id", run["experiment_id"])
        .single()
        .execute()
        .data
    )
    plan_row = (
        admin.table("experiment_plans")
        .select("*")
        .eq("id", run["plan_id"])
        .eq("experiment_id", experiment["id"])
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
    pins = (
        admin.table("execution_run_files")
        .select("generated_file_id")
        .eq("run_id", run_id)
        .execute()
        .data
    )
    file_ids = [p["generated_file_id"] for p in pins]
    code_files: list[CodeFile] = []
    if file_ids:
        rows = (
            admin.table("generated_files")
            .select("path,content,sha256")
            .in_("id", file_ids)
            .execute()
            .data
        )
        code_files = [
            CodeFile(path=r["path"], content=r["content"] or "", sha256=r["sha256"]) for r in rows
        ]
    artifacts = [
        r["artifact_type"]
        for r in admin.table("execution_artifacts")
        .select("artifact_type")
        .eq("run_id", run_id)
        .execute()
        .data
    ]
    audit = RunAudit.model_validate(run["audit_json"]) if run.get("audit_json") else None
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
        plan_version=plan_row["version"],
        code_version=run["code_version"],
        code_hash=run.get("code_hash"),
        run_id=run_id,
        generated_file_ids=file_ids,
        review_trace_ids=[decision.id for decision in review_decisions],
    )
    return EvaluateRequest(
        references=references,
        review_decisions=review_decisions,
        card=ReproductionCard.model_validate(spec_row["spec_json"]),
        plan=ExperimentPlan.model_validate(plan_row["config_json"]),
        paper_title=paper["title"],
        code_files=code_files,
        run_status=RunStatus(run["status"]),
        metrics=run.get("metrics_json") or {},
        audit=audit,
        execution_time=(run.get("runtime_json") or {}).get("execution_time"),
        exit_code=run.get("exit_code"),
        error=run.get("error"),
        error_category=ErrorCategory(run["error_category"]) if run.get("error_category") else None,
        stdout_path=run.get("stdout_path"),
        stderr_path=run.get("stderr_path"),
        artifacts=artifacts,
    )


async def generate_and_store_report(admin, agent, run_id: str) -> dict:
    request = build_evaluate_request(admin, run_id)
    response = await agent.evaluate(request)
    row = {
        "run_id": run_id,
        "report_json": response.report.model_dump(mode="json"),
        "markdown": response.markdown,
    }
    existing = (
        admin.table("reproduction_reports").select("id").eq("run_id", run_id).execute().data
    )
    if existing:
        admin.table("reproduction_reports").update(row).eq("run_id", run_id).execute()
    else:
        admin.table("reproduction_reports").insert(row).execute()
    return row