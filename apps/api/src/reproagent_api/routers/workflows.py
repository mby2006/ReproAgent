from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from reproagent_schemas import ExperimentPlan, SchemaModel, WorkflowStatus

from ..auth import get_current_user_id
from .papers import _admin, _get_paper

router = APIRouter(prefix="/v1", tags=["workflows"])

WORKFLOW_COLUMNS = (
    "id,thread_id,project_id,paper_id,status,current_node,experiment_id,run_id,"
    "spec_version_id,plan_id,budget_json,error,created_at,updated_at"
)
TRACE_COLUMNS = (
    "id,node_name,input_json,output_json,duration_ms,model_name,token_usage,error,"
    "retry_count,created_at"
)


class WorkflowDetail(SchemaModel):
    id: UUID
    thread_id: str
    project_id: UUID
    paper_id: UUID
    status: WorkflowStatus
    current_node: str | None = None
    experiment_id: UUID | None = None
    run_id: UUID | None = None
    spec_version_id: UUID | None = None
    plan_id: UUID | None = None
    budget_json: dict = {}
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class NodeTrace(SchemaModel):
    id: UUID
    node_name: str
    input_json: dict = {}
    output_json: dict = {}
    duration_ms: int | None = None
    model_name: str | None = None
    token_usage: dict | None = None
    error: str | None = None
    retry_count: int = 0
    created_at: datetime


class ResumeRequest(SchemaModel):
    stage: Literal["spec", "plan"]


class ResumeResponse(SchemaModel):
    workflow_id: UUID
    stage: str
    status: str = "queued"


class RetryResponse(SchemaModel):
    workflow_id: UUID
    status: str = "queued"


def _clean(row: dict) -> dict:
    row.pop("projects", None)
    return row


def _get_workflow(admin, workflow_id: UUID, user_id: UUID) -> dict:
    rows = (
        admin.table("workflows")
        .select(f"{WORKFLOW_COLUMNS},projects!inner(user_id)")
        .eq("id", str(workflow_id))
        .eq("projects.user_id", str(user_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return _clean(rows[0])


async def _enqueue_resume(request: Request, workflow_id: UUID, stage: str) -> None:
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Job queue unavailable")
    try:
        await arq.enqueue_job(
            "resume_workflow_task",
            str(workflow_id),
            stage,
            _job_id=f"resume:{workflow_id}:{stage}",
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Failed to enqueue job: {exc}"
        ) from exc


@router.get("/workflows", response_model=list[WorkflowDetail])
async def list_workflows(
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    paper_id: UUID | None = None,
) -> list[WorkflowDetail]:
    admin = _admin(request)
    query = (
        admin.table("workflows")
        .select(f"{WORKFLOW_COLUMNS},projects!inner(user_id)")
        .eq("projects.user_id", str(user_id))
    )
    if paper_id is not None:
        query = query.eq("paper_id", str(paper_id))
    rows = query.order("created_at", desc=True).execute().data
    return [WorkflowDetail.model_validate(_clean(row)) for row in rows]


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(
    request: Request, workflow_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> WorkflowDetail:
    admin = _admin(request)
    return WorkflowDetail.model_validate(_get_workflow(admin, workflow_id, user_id))


@router.get("/workflows/{workflow_id}/traces", response_model=list[NodeTrace])
async def list_traces(
    request: Request, workflow_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[NodeTrace]:
    admin = _admin(request)
    _get_workflow(admin, workflow_id, user_id)
    rows = (
        admin.table("agent_traces")
        .select(TRACE_COLUMNS)
        .eq("workflow_id", str(workflow_id))
        .order("created_at")
        .execute()
        .data
    )
    return [NodeTrace.model_validate(row) for row in rows]


@router.get("/papers/{paper_id}/workflow", response_model=WorkflowDetail)
async def get_paper_workflow(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> WorkflowDetail:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    rows = (
        admin.table("workflows")
        .select(WORKFLOW_COLUMNS)
        .eq("paper_id", str(paper_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No workflow for this paper yet")
    return WorkflowDetail.model_validate(rows[0])


def _check_spec_gate(admin, workflow: dict) -> None:
    if workflow["status"] != WorkflowStatus.AWAITING_SPEC_REVIEW.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow is not awaiting spec review")
    latest = (
        admin.table("reproduction_specs")
        .select("status")
        .eq("paper_id", workflow["paper_id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not latest or latest[0]["status"] != "confirmed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Confirm the reproduction card before continuing"
        )


def _check_plan_gate(admin, workflow: dict) -> None:
    if workflow["status"] != WorkflowStatus.AWAITING_PLAN_APPROVAL.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow is not awaiting plan approval")
    if not workflow.get("experiment_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow has no experiment plan yet")
    latest = (
        admin.table("experiment_plans")
        .select("approved_at,config_json")
        .eq("experiment_id", workflow["experiment_id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not latest or latest[0]["approved_at"] is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Approve the experiment plan before continuing"
        )
    plan = ExperimentPlan.model_validate(latest[0]["config_json"])
    if plan.unconfirmed_required():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Confirm all required plan parameters before continuing"
        )


@router.post("/workflows/{workflow_id}/resume", response_model=ResumeResponse)
async def resume_workflow(
    request: Request,
    workflow_id: UUID,
    body: ResumeRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> ResumeResponse:
    admin = _admin(request)
    workflow = _get_workflow(admin, workflow_id, user_id)
    if body.stage == "spec":
        _check_spec_gate(admin, workflow)
    else:
        _check_plan_gate(admin, workflow)
    await _enqueue_resume(request, workflow_id, body.stage)
    return ResumeResponse(workflow_id=workflow_id, stage=body.stage)


@router.post("/workflows/{workflow_id}/retry", response_model=RetryResponse)
async def retry_workflow(
    request: Request,
    workflow_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> RetryResponse:
    admin = _admin(request)
    workflow = _get_workflow(admin, workflow_id, user_id)
    if workflow["status"] != WorkflowStatus.FAILED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed workflows can be retried")
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Job queue unavailable")
    # recover_workflow_task skips terminal rows; leave "failed" so the checkpoint
    # is re-driven from the node that raised.
    resume_status = (
        WorkflowStatus.EXECUTING.value
        if workflow.get("experiment_id")
        else WorkflowStatus.PARSING.value
    )
    # Atomic claim: only one concurrent retry can flip failed -> running, so two
    # racing retry clicks cannot both enqueue a recovery drive.
    claimed = (
        admin.table("workflows")
        .update({"status": resume_status, "error": None})
        .eq("id", str(workflow_id))
        .eq("status", WorkflowStatus.FAILED.value)
        .execute()
        .data
    )
    if not claimed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed workflows can be retried")
    try:
        await arq.enqueue_job(
            "recover_workflow_task", str(workflow_id), _job_id=f"retry:{workflow_id}"
        )
    except Exception as exc:
        # Roll back the claim so the workflow stays retryable instead of stuck in
        # a running state with no job behind it.
        admin.table("workflows").update(
            {"status": WorkflowStatus.FAILED.value, "error": workflow.get("error")}
        ).eq("id", str(workflow_id)).execute()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Failed to enqueue job: {exc}"
        ) from exc
    return RetryResponse(workflow_id=workflow_id)
