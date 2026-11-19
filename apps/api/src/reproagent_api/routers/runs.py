"""Run orchestration API: start runs, inspect status/logs/metrics/artifacts/files.

Starting a run requires an APPROVED plan. The API only enqueues; the Worker (API
trust domain) drives codegen -> validate -> sandbox execution -> repair loop and
persists every version. Ownership flows runs -> experiments -> projects.user_id.
The API process never executes generated code.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from reproagent_schemas import (
    ErrorCategory,
    RunAccepted,
    RunLogs,
    RunStatus,
    SchemaModel,
)

from ..auth import get_current_user_id
from ..clients.agent import AgentClient
from ..clients.runner import RunnerClient
from ..clients.supabase import make_service_client
from ..services import generate_and_store_report

router = APIRouter(prefix="/v1", tags=["runs"])

TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.TIMEOUT.value,
    RunStatus.CANCELLED.value,
}

RUN_COLUMNS = (
    "id,experiment_id,plan_id,code_version,plan_version,status,exit_code,metrics_json,"
    "runtime_json,stdout_path,stderr_path,started_at,finished_at,created_at,updated_at,"
    "error_category,error,repair_attempt,code_hash,runner_run_id,audit_json"
)

SUMMARY_COLUMNS = (
    "id,experiment_id,plan_id,code_version,plan_version,status,repair_attempt,exit_code,"
    "error_category,created_at,started_at,finished_at,updated_at"
)


class RunSummary(SchemaModel):
    id: UUID
    experiment_id: UUID
    plan_id: UUID
    code_version: int
    plan_version: int
    status: RunStatus
    repair_attempt: int
    exit_code: int | None = None
    error_category: ErrorCategory | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class RunDetail(RunSummary):
    error: str | None = None
    code_hash: str | None = None
    runner_run_id: UUID | None = None
    metrics_json: dict = {}
    audit_json: dict = {}
    runtime_json: dict = {}
    stdout_path: str | None = None
    stderr_path: str | None = None


class ArtifactRecord(SchemaModel):
    id: UUID
    artifact_type: str
    storage_path: str
    metadata_json: dict = {}
    created_at: datetime


class CodeFileRecord(SchemaModel):
    path: str
    version: int
    sha256: str
    content: str | None = None
    change_reason: str | None = None
    related_error: str | None = None


class RunReport(SchemaModel):
    run_id: UUID
    report_json: dict
    markdown: str
    created_at: datetime


def _admin(request: Request):
    return make_service_client(request.app.state.settings)


def _get_experiment(admin, experiment_id: UUID, user_id: UUID) -> dict:
    rows = (
        admin.table("experiments")
        .select("*, projects!inner(user_id)")
        .eq("id", str(experiment_id))
        .eq("projects.user_id", str(user_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Experiment not found")
    return rows[0]


def _get_run(admin, run_id: UUID, user_id: UUID) -> dict:
    rows = (
        admin.table("execution_runs")
        .select(f"{RUN_COLUMNS}, experiments!inner(project_id, projects!inner(user_id))")
        .eq("id", str(run_id))
        .eq("experiments.projects.user_id", str(user_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    row = rows[0]
    return {col.strip(): row.get(col.strip()) for col in RUN_COLUMNS.split(",")}


def _latest_approved_plan(admin, experiment_id: str) -> dict | None:
    rows = (
        admin.table("experiment_plans")
        .select("id,version,config_json,approved_at")
        .eq("experiment_id", experiment_id)
        .not_.is_("approved_at", "null")
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


async def _enqueue(request: Request, run_id: UUID) -> None:
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Job queue unavailable")
    try:
        await arq.enqueue_job("run_experiment_task", str(run_id))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Failed to enqueue job: {exc}"
        ) from exc


@router.post(
    "/experiments/{experiment_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    request: Request,
    experiment_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> RunAccepted:
    admin = _admin(request)
    experiment = _get_experiment(admin, experiment_id, user_id)
    plan = _latest_approved_plan(admin, str(experiment_id))
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "请先审批实验计划后再执行")
    if not experiment.get("spec_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "实验缺少 Reproduction Card")

    run_id = uuid4()
    admin.table("execution_runs").insert(
        {
            "id": str(run_id),
            "experiment_id": str(experiment_id),
            "plan_id": plan["id"],
            "code_version": 1,
            "plan_version": plan["version"],
            "status": RunStatus.QUEUED.value,
            "repair_attempt": 0,
        }
    ).execute()
    admin.table("experiments").update({"status": "running"}).eq("id", str(experiment_id)).execute()
    await _enqueue(request, run_id)
    return RunAccepted(run_id=run_id, status=RunStatus.QUEUED)


@router.get("/experiments/{experiment_id}/runs", response_model=list[RunSummary])
async def list_runs(
    request: Request,
    experiment_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> list[RunSummary]:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    rows = (
        admin.table("execution_runs")
        .select(SUMMARY_COLUMNS)
        .eq("experiment_id", str(experiment_id))
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return [RunSummary.model_validate(row) for row in rows]


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> RunDetail:
    admin = _admin(request)
    return RunDetail.model_validate(_get_run(admin, run_id, user_id))


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactRecord])
async def get_artifacts(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[ArtifactRecord]:
    admin = _admin(request)
    _get_run(admin, run_id, user_id)
    rows = (
        admin.table("execution_artifacts")
        .select("id,artifact_type,storage_path,metadata_json,created_at")
        .eq("run_id", str(run_id))
        .order("created_at")
        .execute()
        .data
    )
    return [ArtifactRecord.model_validate(row) for row in rows]


@router.get("/runs/{run_id}/files", response_model=list[CodeFileRecord])
async def get_files(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[CodeFileRecord]:
    admin = _admin(request)
    _get_run(admin, run_id, user_id)
    pins = (
        admin.table("execution_run_files")
        .select("generated_file_id")
        .eq("run_id", str(run_id))
        .execute()
        .data
    )
    if not pins:
        return []
    file_ids = [p["generated_file_id"] for p in pins]
    rows = (
        admin.table("generated_files")
        .select("path,version,sha256,content,change_reason,related_error")
        .in_("id", file_ids)
        .execute()
        .data
    )
    return [CodeFileRecord.model_validate(row) for row in rows]


@router.get("/runs/{run_id}/logs", response_model=RunLogs)
async def get_logs(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> RunLogs:
    admin = _admin(request)
    run = _get_run(admin, run_id, user_id)
    # While the run is live, logs only exist inside the Runner's container;
    # Storage is written after the run ends. Fall back to Storage if unreachable.
    if run["status"] not in TERMINAL_STATUSES and run.get("runner_run_id"):
        runner = RunnerClient(
            request.app.state.settings,
            transport=getattr(request.app.state, "runner_transport", None),
        )
        try:
            return await runner.get_run_logs(UUID(run["runner_run_id"]))
        except Exception:
            pass
        finally:
            await runner.aclose()
    bucket = admin.storage.from_("runs")

    def read(path: str | None) -> str:
        if not path:
            return ""
        try:
            return bucket.download(path).decode("utf-8", "replace")
        except Exception:
            return ""

    return RunLogs(stdout=read(run.get("stdout_path")), stderr=read(run.get("stderr_path")))


@router.get("/runs/{run_id}/report", response_model=RunReport)
async def get_report(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> RunReport:
    admin = _admin(request)
    _get_run(admin, run_id, user_id)
    rows = (
        admin.table("reproduction_reports")
        .select("run_id,report_json,markdown,created_at")
        .eq("run_id", str(run_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "报告尚未生成")
    return RunReport.model_validate(rows[0])


@router.post("/runs/{run_id}/report", response_model=RunReport)
async def regenerate_report(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> RunReport:
    admin = _admin(request)
    run = _get_run(admin, run_id, user_id)
    if run["status"] not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "运行尚未结束，无法生成报告")
    agent = AgentClient(
        request.app.state.settings,
        transport=getattr(request.app.state, "agent_transport", None),
    )
    try:
        await generate_and_store_report(admin, agent, str(run_id))
    finally:
        await agent.aclose()
    rows = (
        admin.table("reproduction_reports")
        .select("run_id,report_json,markdown,created_at")
        .eq("run_id", str(run_id))
        .execute()
        .data
    )
    return RunReport.model_validate(rows[0])


@router.post("/runs/{run_id}/cancel", response_model=RunDetail)
async def cancel_run(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> RunDetail:
    admin = _admin(request)
    run = _get_run(admin, run_id, user_id)
    if run["status"] in TERMINAL_STATUSES:
        return RunDetail.model_validate(run)
    if run.get("runner_run_id"):
        runner = RunnerClient(
            request.app.state.settings,
            transport=getattr(request.app.state, "runner_transport", None),
        )
        try:
            await runner.cancel_run(UUID(run["runner_run_id"]))
        except Exception as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Failed to cancel run on runner: {exc}"
            ) from exc
        finally:
            await runner.aclose()
    # Conditional flip: only queued/running rows become cancelled, so a run that
    # finished concurrently keeps its real terminal status instead of being
    # overwritten.
    updated = (
        admin.table("execution_runs")
        .update({"status": RunStatus.CANCELLED.value})
        .eq("id", str(run_id))
        .in_("status", [RunStatus.QUEUED.value, RunStatus.RUNNING.value])
        .execute()
        .data
    )
    if not updated:
        return RunDetail.model_validate(_get_run(admin, run_id, user_id))
    run["status"] = RunStatus.CANCELLED.value
    return RunDetail.model_validate(run)
