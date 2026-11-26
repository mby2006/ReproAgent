"""Run orchestration API: start runs, inspect status/logs/metrics/artifacts/files.

Starting a run requires an APPROVED plan. The API only enqueues; the Worker (API
trust domain) drives codegen -> validate -> sandbox execution -> repair loop and
persists every version. Ownership flows runs -> experiments -> projects.user_id.
The API process never executes generated code.
"""

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from httpx import HTTPStatusError
from postgrest.exceptions import APIError as PostgrestAPIError
from pydantic import Field, ValidationError
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
from ..services.dispatch import enqueue_pending_outbox
from ..services.runs import (
    execution_request_id,
    finalize_execution_run,
    load_execution_plan,
    run_creation_fingerprint,
)

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


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")

_RPC_ERROR_MESSAGES = {
    "FORBIDDEN": "无权操作此实验",
    "STALE_PLAN_APPROVAL": "只有最新且已批准的实验计划可以运行",
    "PLAN_NOT_APPROVED": "实验计划尚未批准",
    "PLAN_NOT_READY": "实验计划未满足就绪条件",
    "DELEGATION_ACTOR_MISMATCH": "委托授权与当前操作者不匹配",
    "ACTIVE_RUN_EXISTS": "实验已有活跃的运行",
    "IDEMPOTENCY_CONFLICT": "幂等键冲突",
}


def _rpc_error_code(exc: PostgrestAPIError) -> str | None:
    """Read the authorization code the ``create_execution_run`` RPC raised.

    The RPC signals every authorization rejection with
    ``RAISE EXCEPTION '<CODE>' USING errcode = 'P0001'`` (42501 for FORBIDDEN).
    PostgREST wraps that into ``APIError``: ``exc.message`` is the bare ``RAISE``
    text, while ``exc.code`` is only the SQLSTATE, which is shared by all the
    ``P0001`` checks and therefore cannot tell PLAN_NOT_APPROVED from
    PLAN_NOT_READY. So the symbolic name is taken from the structured ``message``
    field, matched against the exact known vocabulary (never a loose substring of
    an arbitrary repr). Anything unrecognised is not an authorization decision and
    returns ``None`` so the caller surfaces it as a server error instead of a 409.
    """
    message = (exc.message or "").strip()
    return message if message in _RPC_ERROR_MESSAGES else None


@router.post(
    "/experiments/{experiment_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    request: Request,
    experiment_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RunAccepted:
    # Validate idempotency key presence and format.
    if idempotency_key is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required"},
        )
    if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "IDEMPOTENCY_KEY_INVALID",
             "message": "Idempotency-Key must be 8-128 chars of [A-Za-z0-9:_-]"},
        )

    admin = _admin(request)
    experiment = _get_experiment(admin, experiment_id, user_id)
    if not experiment.get("spec_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "实验缺少 Reproduction Card")
    try:
        _, plan, _, _, _, actor = load_execution_plan(admin, str(experiment_id))
    except ValueError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "请先确认 Card 并审批最新实验计划，托管授权必须属于项目所有者",
        ) from None
    if actor != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Experiment not found")

    fingerprint = run_creation_fingerprint(experiment_id, UUID(plan["id"]), plan["version"])

    # Catch only the structured PostgREST authorization failure. A transport or
    # programming error is not a client conflict and must not be turned into a 409.
    try:
        result = admin.rpc("create_execution_run", {
            "p_experiment_id": str(experiment_id),
            "p_plan_id": plan["id"],
            "p_plan_version": plan["version"],
            "p_actor_id": str(user_id),
            "p_idempotency_key": idempotency_key,
            "p_request_fingerprint": fingerprint,
        })
    except PostgrestAPIError as exc:
        code = _rpc_error_code(exc)
        if code is None:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"code": "RUN_CREATION_FAILED", "message": "运行创建失败"},
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": code, "message": _RPC_ERROR_MESSAGES[code]},
        ) from exc

    # A malformed run identifier from a successful RPC is a server-side fault, not
    # an authorization decision, so it surfaces as 500 rather than being swallowed
    # into the 409 mapping above.
    try:
        run_id = UUID(str(result))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"code": "RUN_CREATION_FAILED", "message": "运行创建失败"},
        ) from exc

    # Redis is only a delivery optimization. The run and its outbox intent are
    # already durable, so an outage must not turn an accepted run into a 503.
    await enqueue_pending_outbox(
        admin,
        getattr(request.app.state, "arq", None),
        entity_id=run_id,
    )
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


class PredictionExample(SchemaModel):
    index: int = Field(ge=0)
    pixels: str = Field(pattern=r"^[0-9a-f]{1568}$")
    label: int = Field(ge=0, le=9)
    prediction: int = Field(ge=0, le=9)


class PredictionExamples(SchemaModel):
    version: Literal[1]
    samples: list[PredictionExample] = Field(min_length=1, max_length=12)


@router.get("/runs/{run_id}/prediction-examples", response_model=PredictionExamples)
async def get_prediction_examples(
    request: Request, run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> PredictionExamples:
    admin = _admin(request)
    run = _get_run(admin, run_id, user_id)
    if run["status"] != RunStatus.SUCCEEDED.value:
        raise HTTPException(404, "本次运行没有预测样本")
    path = f"{run_id}/attempt-{run['repair_attempt']}/artifacts/prediction_examples.json"
    rows = (admin.table("execution_artifacts").select("storage_path")
            .eq("run_id", str(run_id)).eq("storage_path", path).execute().data)
    if not rows:
        raise HTTPException(404, "这次运行未保存逐样本预测")
    data = admin.storage.from_("runs").download(path)
    try:
        return PredictionExamples.model_validate_json(data)
    except ValidationError as exc:
        raise HTTPException(502, "该运行的预测样本格式无效") from exc


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

    # Dispatch can persist a Runner id while cancellation is awaiting the Runner.
    # Re-read after every await and cancel whichever identity is currently known;
    # an executing row with no id still has a deterministic acceptance identity.
    cancelled: set[UUID] = set()
    while True:
        current = _get_run(admin, run_id, user_id)
        if current["status"] in TERMINAL_STATUSES:
            return RunDetail.model_validate(current)
        known_id = current.get("runner_run_id")
        identity = UUID(known_id) if known_id else None
        if identity is None and current["status"] in (
            RunStatus.DISPATCHING.value,
            RunStatus.RUNNING.value,
        ):
            identity = execution_request_id(run_id, current.get("repair_attempt", 0))
        if identity is None or identity in cancelled:
            # This read is immediately before the guarded write, with no await
            # between them, so a real terminal result is returned rather than
            # overwritten and no newly discovered identity is left uncancelled.
            finalize_execution_run(
                admin,
                str(run_id),
                RunStatus.CANCELLED,
                None,
                attempt=current.get("repair_attempt", 0),
                require_latest=True,
            )
            return RunDetail.model_validate(_get_run(admin, run_id, user_id))

        runner = RunnerClient(
            request.app.state.settings,
            transport=getattr(request.app.state, "runner_transport", None),
        )
        try:
            await runner.cancel_run(identity)
        except HTTPStatusError as exc:
            # A deterministic identity may not have reached Runner yet. A known
            # Runner id missing from the Registry remains an operational error.
            if exc.response.status_code != 404 or known_id:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Failed to cancel run on runner: {exc}",
                ) from exc
        except Exception as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Failed to cancel run on runner: {exc}"
            ) from exc
        finally:
            await runner.aclose()
        cancelled.add(identity)
