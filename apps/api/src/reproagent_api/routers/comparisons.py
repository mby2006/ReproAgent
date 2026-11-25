"""Paired comparison API: candidates -> plan -> pending items -> approve -> run.

Ownership is resolved through papers -> projects.user_id on every endpoint, the
same ownership root the single-group experiments use. Execution itself is
delegated to the worker queue; this router never trains or generates code
in-process.
"""

import io
import json
import zipfile
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import Field
from reproagent_schemas import (
    OPTIMIZER_NAMES,
    CandidateRequest,
    CandidateResponse,
    ComparisonGroup,
    ComparisonOrigin,
    ComparisonOutcome,
    ComparisonPlan,
    ComparisonPlanRequest,
    ComparisonRerunRequest,
    ComparisonStatus,
    GroupKey,
    PaperReference,
    ParsedDocument,
    PendingDecision,
    PlanVersionOrigin,
    ReproductionCard,
    RunStatus,
    SchemaModel,
)

from ..auth import get_current_user_id
from ..clients.agent import AgentClient
from ..clients.supabase import make_service_client
from ..services import comparisons as service

router = APIRouter(prefix="/v1", tags=["comparisons"])


class GroupInput(SchemaModel):
    key: GroupKey
    label: str = Field(min_length=1, max_length=40)
    optimizer: str = Field(min_length=1, max_length=40)
    learning_rate: float | None = Field(default=None, gt=0)
    weight_decay: float | None = Field(default=None, ge=0)
    references: list[PaperReference] = Field(default_factory=list)
    reported_result: PaperReference | None = None


class CandidateQuery(SchemaModel):
    question: str | None = None


class CreatePlanRequest(SchemaModel):
    question: str = Field(min_length=4, max_length=500)
    groups: list[GroupInput] = Field(min_length=2, max_length=2)
    name: str | None = Field(default=None, max_length=200)
    seeds: list[int] | None = None
    train_subset_size: int | None = Field(default=None, gt=0)
    test_subset_size: int | None = Field(default=None, gt=0)
    epochs: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    max_seconds: int | None = Field(default=None, gt=0)


class RevisePlanRequest(SchemaModel):
    base_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=500)
    question: str | None = Field(default=None, max_length=500)
    adopt_pending_ids: list[str] = Field(default_factory=list, max_length=20)
    provided_pending: dict[
        Annotated[str, Field(min_length=1, max_length=200)],
        Annotated[str, Field(min_length=1, max_length=500)],
    ] = Field(default_factory=dict, max_length=20)
    train_subset_size: int | None = Field(default=None, gt=0)
    test_subset_size: int | None = Field(default=None, gt=0)
    epochs: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)


class VersionRequest(SchemaModel):
    base_version: int = Field(ge=1)


class VersionSummary(SchemaModel):
    id: UUID
    version: int
    origin: str
    approved_at: datetime | None = None
    change_reason: str | None = None
    created_at: datetime | None = None


class ComparisonSummary(SchemaModel):
    id: UUID
    status: ComparisonStatus
    origin: str
    completed_pairs: int = 0
    requested_pairs: int = 1
    error: str | None = None
    created_at: datetime | None = None


class PlanDetail(SchemaModel):
    id: UUID
    paper_id: UUID
    name: str
    status: str
    latest_version: int
    latest_version_id: UUID
    approved_version: int | None = None
    plan: ComparisonPlan
    versions: list[VersionSummary] = Field(default_factory=list)
    comparisons: list[ComparisonSummary] = Field(default_factory=list)


class RunView(SchemaModel):
    id: UUID
    seed: int
    group_key: GroupKey
    attempt: int
    status: RunStatus
    accuracy: float | None = None
    correct_count: int | None = None
    evaluated_count: int | None = None
    metric_value: float | None = None
    error: str | None = None
    code_hash: str | None = None
    image_digest: str | None = None


class ComparisonDetail(SchemaModel):
    id: UUID
    plan_id: UUID
    plan_version: int
    plan_version_id: UUID
    status: ComparisonStatus
    origin: str
    source_comparison_id: UUID | None = None
    question: str | None = None
    requested_pairs: int = 1
    completed_pairs: int = 0
    groups: list[ComparisonGroup] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)
    runs: list[RunView] = Field(default_factory=list)
    outcome: ComparisonOutcome | None = None
    report_status: str | None = None
    error: str | None = None
    created_at: datetime | None = None


class StartResponse(SchemaModel):
    comparison_id: UUID
    status: ComparisonStatus
    warnings: list[str] = Field(default_factory=list)


def _agent_error(exc: Exception) -> str | dict:
    """Surface the agent's structured 422 (missing support items) unchanged."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            payload = response.json()
            detail = payload.get("detail")
            if isinstance(detail, dict):
                return detail
            if isinstance(detail, str):
                return detail
        except Exception:
            pass
    return str(exc)


def _admin(request: Request):
    return make_service_client(request.app.state.settings)


def _agent(request: Request) -> AgentClient:
    return AgentClient(
        request.app.state.settings,
        transport=getattr(request.app.state, "agent_transport", None),
    )


def _get_paper(admin, paper_id: UUID, user_id: UUID) -> dict:
    rows = (
        admin.table("papers")
        .select("*, projects!inner(user_id)")
        .eq("id", str(paper_id))
        .eq("projects.user_id", str(user_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    return rows[0]


def _get_plan(admin, plan_id: UUID, user_id: UUID) -> dict:
    rows = admin.table("comparison_plans").select("*").eq("id", str(plan_id)).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对照计划不存在")
    plan_row = rows[0]
    owners = (
        admin.table("projects")
        .select("id")
        .eq("id", plan_row["project_id"])
        .eq("user_id", str(user_id))
        .execute()
        .data
    )
    if not owners:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对照计划不存在")
    return plan_row


def _get_comparison(admin, comparison_id: UUID, user_id: UUID) -> dict:
    rows = admin.table("comparisons").select("*").eq("id", str(comparison_id)).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对照任务不存在")
    comparison = rows[0]
    _get_plan(admin, UUID(comparison["plan_id"]), user_id)
    return comparison


def _confirmed_card(admin, paper_id: str) -> tuple[dict, ReproductionCard]:
    rows = (
        admin.table("reproduction_specs")
        .select("*")
        .eq("paper_id", paper_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows or rows[0].get("status") != "confirmed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "请先确认 Reproduction Card，再建立对照计划"
        )
    return rows[0], ReproductionCard.model_validate(rows[0]["spec_json"])


def _document(admin, paper_id: str):
    rows = (
        admin.table("understanding_jobs")
        .select("document")
        .eq("paper_id", paper_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows or not rows[0].get("document"):
        return None
    try:
        return ParsedDocument.model_validate(rows[0]["document"])
    except Exception:
        return None


def _plan_detail(admin, plan_row: dict) -> PlanDetail:
    version_row, plan = service.latest_version(admin, plan_row["id"])
    versions = service.list_versions(admin, plan_row["id"])
    approved = [row for row in versions if row.get("approved_at")]
    comparisons = (
        admin.table("comparisons")
        .select("id,status,origin,completed_pairs,requested_pairs,error,created_at")
        .eq("plan_id", plan_row["id"])
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return PlanDetail(
        id=UUID(plan_row["id"]),
        paper_id=UUID(plan_row["paper_id"]),
        name=plan_row["name"],
        status=plan_row["status"],
        latest_version=version_row["version"],
        latest_version_id=UUID(version_row["id"]),
        approved_version=max((row["version"] for row in approved), default=None),
        plan=plan,
        versions=[VersionSummary.model_validate(row) for row in versions],
        comparisons=[
            ComparisonSummary.model_validate(
                {
                    **row,
                    "completed_pairs": row.get("completed_pairs") or 0,
                    "requested_pairs": row.get("requested_pairs") or 1,
                }
            )
            for row in comparisons
        ],
    )


async def _enqueue(request: Request, comparison_id: UUID) -> None:
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "任务队列不可用")
    try:
        # Let arq assign a fresh identity so a retained failed/no-op result cannot
        # suppress retry delivery; the advisory lock and state guards deduplicate.
        await arq.enqueue_job("run_comparison_task", str(comparison_id))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"任务入队失败：{exc}"
        ) from exc


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------


@router.post("/papers/{paper_id}/comparison-candidates", response_model=CandidateResponse)
async def comparison_candidates(
    request: Request,
    paper_id: UUID,
    body: CandidateQuery,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> CandidateResponse:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    _, card = _confirmed_card(admin, str(paper_id))
    agent = _agent(request)
    try:
        return await agent.comparison_candidates(
            CandidateRequest(
                card=card, document=_document(admin, str(paper_id)), question=body.question
            )
        )
    finally:
        await agent.aclose()


# --------------------------------------------------------------------------
# Plan lifecycle
# --------------------------------------------------------------------------


@router.post(
    "/papers/{paper_id}/comparison-plans",
    response_model=PlanDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_comparison_plan(
    request: Request,
    paper_id: UUID,
    body: CreatePlanRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanDetail:
    admin = _admin(request)
    paper = _get_paper(admin, paper_id, user_id)
    spec_row, card = _confirmed_card(admin, str(paper_id))
    agent = _agent(request)
    try:
        response = await agent.comparison_plan(
            ComparisonPlanRequest(
                card=card,
                question=body.question,
                group_specs=[
                    {
                        "key": group.key.value,
                        "label": group.label,
                        "optimizer": group.optimizer,
                        "learning_rate": group.learning_rate,
                        "weight_decay": group.weight_decay,
                        "references": group.references,
                        "reported_result": group.reported_result,
                    }
                    for group in body.groups
                ],
                seeds=body.seeds,
                train_subset_size=body.train_subset_size,
                test_subset_size=body.test_subset_size,
                epochs=body.epochs,
                batch_size=body.batch_size,
                max_seconds=body.max_seconds,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, _agent_error(exc)
        ) from exc
    finally:
        await agent.aclose()

    name = body.name or (
        f"{paper['title']} · "
        + " vs ".join(group.optimizer.upper() for group in body.groups)
    )
    plan_row, _ = service.create_plan(
        admin,
        project_id=paper["project_id"],
        paper_id=str(paper_id),
        spec_id=spec_row["id"],
        name=name,
        plan=response.plan,
    )
    return _plan_detail(admin, plan_row)


@router.get("/comparison-plans/{plan_id}", response_model=PlanDetail)
async def get_comparison_plan(
    request: Request,
    plan_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanDetail:
    admin = _admin(request)
    return _plan_detail(admin, _get_plan(admin, plan_id, user_id))


@router.post("/comparison-plans/{plan_id}/versions", response_model=PlanDetail)
async def revise_comparison_plan(
    request: Request,
    plan_id: UUID,
    body: RevisePlanRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanDetail:
    admin = _admin(request)
    plan_row = _get_plan(admin, plan_id, user_id)
    version_row, plan = service.latest_version(admin, plan_row["id"])
    if version_row["version"] != body.base_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "计划已更新，请基于最新版本修改"
        )
    if (
        not (body.question or "").strip()
        and not body.adopt_pending_ids
        and not body.provided_pending
        and all(
            value is None
            for value in (
                body.train_subset_size,
                body.test_subset_size,
                body.epochs,
                body.batch_size,
            )
        )
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要保存的修改")

    question_changed = bool(body.question) and body.question.strip() != plan.question.question
    if question_changed and (
        body.adopt_pending_ids
        or body.provided_pending
        or any(
            value is not None
            for value in (
                body.train_subset_size,
                body.test_subset_size,
                body.epochs,
                body.batch_size,
            )
        )
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "改题会重新规划，请先单独提交新问题，再确认参数或调整规模",
        )
    if question_changed:
        spec_rows = (
            admin.table("reproduction_specs")
            .select("*")
            .eq("id", plan_row["spec_id"])
            .execute()
            .data
        )
        if not spec_rows:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "对照计划固定的 Card 版本不存在，请重新建立对照计划"
            )
        card = ReproductionCard.model_validate(spec_rows[0]["spec_json"])
        agent = _agent(request)
        try:
            response = await agent.comparison_plan(
                ComparisonPlanRequest(
                    card=card,
                    question=body.question.strip(),
                    group_specs=[
                        {
                            "key": group.key.value,
                            "label": group.label,
                            "optimizer": group.optimizer.value,
                            "learning_rate": group.learning_rate.value,
                            "weight_decay": (
                                group.weight_decay.value if group.weight_decay else None
                            ),
                            "references": group.references,
                            "reported_result": group.reported_result,
                        }
                        for group in plan.groups
                    ],
                    seeds=plan.seeds,
                    train_subset_size=plan.base_plan.dataset_config.train_subset_size,
                    test_subset_size=plan.base_plan.dataset_config.test_subset_size,
                    epochs=plan.base_plan.training_config.epochs.value,
                    batch_size=plan.base_plan.training_config.batch_size.value,
                )
            )
        except Exception as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, _agent_error(exc)
            ) from exc
        finally:
            await agent.aclose()
        new_plan = response.plan
    else:
        decisions = [
            PendingDecision(pending_id=pending_id, adopt_suggestion=True)
            for pending_id in body.adopt_pending_ids
        ] + [
            PendingDecision(pending_id=pending_id, value=value, source="用户补充依据")
            for pending_id, value in body.provided_pending.items()
        ]
        new_plan = plan
        try:
            if decisions:
                new_plan = service.apply_pending_decisions(
                    new_plan, decisions, OPTIMIZER_NAMES
                )
            scaling_requested = any(
                value is not None
                for value in (
                    body.train_subset_size,
                    body.test_subset_size,
                    body.epochs,
                    body.batch_size,
                )
            )
            if scaling_requested:
                new_plan, _beyond = service.apply_scaling_change(
                    new_plan,
                    train_subset_size=body.train_subset_size,
                    test_subset_size=body.test_subset_size,
                    epochs=body.epochs,
                    batch_size=body.batch_size,
                )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if question_changed:
        service.supersede_open_comparisons(
            admin, plan_row["id"], "问题已变更：原问题保留为未完成，请对新的对照计划重新审批"
        )
    origin = (
        PlanVersionOrigin.USER_EDIT if not question_changed else PlanVersionOrigin.GENERATED
    )
    service.add_version(
        admin,
        plan_row,
        new_plan,
        origin=origin,
        change_reason=body.change_reason,
        parent_version_id=version_row["id"],
    )
    admin.table("comparison_plans").update(
        {"status": "awaiting_confirmation"}
    ).eq("id", plan_row["id"]).execute()
    return _plan_detail(admin, plan_row)


@router.post(
    "/comparison-plans/{plan_id}/versions/{version}/approve", response_model=PlanDetail
)
async def approve_comparison_plan(
    request: Request,
    plan_id: UUID,
    version: int,
    body: VersionRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanDetail:
    admin = _admin(request)
    plan_row = _get_plan(admin, plan_id, user_id)
    version_row, plan = service.latest_version(admin, plan_row["id"])
    if version_row["version"] != version or body.base_version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, "只能批准最新版本，且版本号需一致")
    try:
        service.approve_version(admin, plan_row, version_row, plan)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _plan_detail(admin, plan_row)


@router.post(
    "/comparison-plans/{plan_id}/versions/{version}/start",
    response_model=StartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_comparison(
    request: Request,
    plan_id: UUID,
    version: int,
    body: VersionRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StartResponse:
    admin = _admin(request)
    plan_row = _get_plan(admin, plan_id, user_id)
    version_row, plan = service.latest_version(admin, plan_row["id"])
    if version_row["version"] != version or body.base_version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, "只能启动最新版本，且版本号需一致")
    if not version_row.get("approved_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "请先批准该版本再启动对照")
    if not plan.approval_ready():
        raise HTTPException(status.HTTP_409_CONFLICT, "仍有未确认的关键参数")
    violations = service.authorization_violations(plan)
    if violations:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "计划超出授权范围：" + "；".join(violations)
        )

    comparison = service.create_comparison(
        admin,
        plan_row=plan_row,
        version_row=version_row,
        plan=plan,
        origin=ComparisonOrigin.NEW,
        source_comparison_id=None,
        idempotency_key=idempotency_key,
    )
    await _enqueue(request, UUID(comparison["id"]))
    return StartResponse(
        comparison_id=UUID(comparison["id"]),
        status=ComparisonStatus(comparison["status"]),
    )


# --------------------------------------------------------------------------
# Comparison inspection / export / rerun
# --------------------------------------------------------------------------


@router.get("/comparisons/{comparison_id}", response_model=ComparisonDetail)
async def get_comparison(
    request: Request,
    comparison_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> ComparisonDetail:
    admin = _admin(request)
    comparison = _get_comparison(admin, comparison_id, user_id)
    version_row = service.get_version_row(admin, comparison["plan_version_id"])
    plan = ComparisonPlan.model_validate(version_row["plan_json"])
    rows = service.comparison_runs(admin, str(comparison_id))
    runs: list[RunView] = []
    for row in rows:
        run_rows = (
            admin.table("execution_runs")
            .select("id,status,metrics_json,audit_json,code_hash,error")
            .eq("id", row["run_id"])
            .execute()
            .data
        )
        if not run_rows:
            continue
        run = run_rows[0]
        metrics = run.get("metrics_json") or {}
        runs.append(
            RunView(
                id=UUID(run["id"]),
                seed=row["seed"],
                group_key=GroupKey(row["group_key"]),
                attempt=row["attempt"],
                status=RunStatus(run["status"]),
                accuracy=metrics.get("accuracy"),
                correct_count=metrics.get("correct_count"),
                evaluated_count=metrics.get("evaluated_count"),
                metric_value=metrics.get("metric_value"),
                error=run.get("error"),
                code_hash=run.get("code_hash"),
                image_digest=(run.get("audit_json") or {}).get("image_digest"),
            )
        )
    reports = (
        admin.table("comparison_reports")
        .select("status")
        .eq("comparison_id", str(comparison_id))
        .execute()
        .data
    )
    outcome = None
    if comparison.get("summary_json"):
        try:
            outcome = ComparisonOutcome.model_validate(comparison["summary_json"])
        except Exception:
            outcome = None
    return ComparisonDetail(
        id=UUID(comparison["id"]),
        plan_id=UUID(comparison["plan_id"]),
        plan_version=version_row["version"],
        plan_version_id=UUID(version_row["id"]),
        status=ComparisonStatus(comparison["status"]),
        origin=comparison.get("origin", "new"),
        source_comparison_id=(
            UUID(comparison["source_comparison_id"])
            if comparison.get("source_comparison_id")
            else None
        ),
        question=comparison.get("question"),
        requested_pairs=comparison.get("requested_pairs") or 1,
        completed_pairs=comparison.get("completed_pairs") or 0,
        groups=plan.groups,
        seeds=plan.seeds,
        runs=runs,
        outcome=outcome,
        report_status=reports[0]["status"] if reports else None,
        error=comparison.get("error"),
        created_at=comparison.get("created_at"),
    )


@router.get("/comparisons/{comparison_id}/report")
async def get_comparison_report(
    request: Request,
    comparison_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> dict:
    admin = _admin(request)
    _get_comparison(admin, comparison_id, user_id)
    rows = (
        admin.table("comparison_reports")
        .select("report_json,markdown,status,created_at")
        .eq("comparison_id", str(comparison_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对照报告尚未生成")
    row = rows[0]
    return {
        "comparison_id": str(comparison_id),
        "status": row["status"],
        "report_json": row["report_json"],
        "markdown": row["markdown"],
        "created_at": row["created_at"],
    }


@router.get("/comparisons/{comparison_id}/export")
async def export_comparison(
    request: Request,
    comparison_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> Response:
    """Artifact package: configs, code, logs, metrics, model files, audit trail."""
    admin = _admin(request)
    comparison = _get_comparison(admin, comparison_id, user_id)
    version_row = service.get_version_row(admin, comparison["plan_version_id"])
    plan = ComparisonPlan.model_validate(version_row["plan_json"])
    report_rows = (
        admin.table("comparison_reports")
        .select("report_json,markdown")
        .eq("comparison_id", str(comparison_id))
        .execute()
        .data
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "comparison_id": str(comparison_id),
                    "plan_version": version_row["version"],
                    "status": comparison["status"],
                    "seeds": plan.seeds,
                    "groups": [
                        {
                            "key": group.key.value,
                            "label": group.label,
                            "optimizer": group.optimizer.value,
                            "learning_rate": group.learning_rate.value,
                        }
                        for group in plan.groups
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr(
            "plan.json",
            json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        if report_rows:
            archive.writestr("report.md", report_rows[0]["markdown"])
            archive.writestr(
                "report.json",
                json.dumps(report_rows[0]["report_json"], ensure_ascii=False, indent=2),
            )
        rows = service.comparison_runs(admin, str(comparison_id))
        for row in rows:
            runs = (
                admin.table("execution_runs")
                .select(
                    "id,status,metrics_json,audit_json,code_hash,stdout_path,stderr_path,error"
                )
                .eq("id", row["run_id"])
                .execute()
                .data
            )
            if not runs:
                continue
            run = runs[0]
            prefix = f"runs/seed-{row['seed']}-{row['group_key']}-attempt-{row['attempt']}"
            archive.writestr(
                f"{prefix}/run.json",
                json.dumps(
                    {
                        "run_id": run["id"],
                        "status": run["status"],
                        "code_hash": run.get("code_hash"),
                        "audit": run.get("audit_json") or {},
                        "metrics": run.get("metrics_json") or {},
                        "error": run.get("error"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            for label, path in (("stdout.txt", run.get("stdout_path")),
                                ("stderr.txt", run.get("stderr_path"))):
                if not path:
                    continue
                try:
                    data = admin.storage.from_("runs").download(path)
                    archive.writestr(f"{prefix}/{label}", data)
                except Exception:
                    archive.writestr(f"{prefix}/{label}", "storage object unavailable")
            pins = (
                admin.table("execution_run_files")
                .select("generated_file_id")
                .eq("run_id", run["id"])
                .execute()
                .data
            )
            if pins:
                file_rows = (
                    admin.table("generated_files")
                    .select("path,content,sha256")
                    .in_("id", [pin["generated_file_id"] for pin in pins])
                    .execute()
                    .data
                )
                for file_row in file_rows:
                    archive.writestr(
                        f"{prefix}/code/{file_row['path']}", file_row.get("content") or ""
                    )
            artifacts = (
                admin.table("execution_artifacts")
                .select("artifact_type,storage_path")
                .eq("run_id", run["id"])
                .execute()
                .data
            )
            for artifact in artifacts:
                try:
                    data = admin.storage.from_("runs").download(artifact["storage_path"])
                except Exception:
                    continue
                archive.writestr(
                    f"{prefix}/artifacts/{artifact['artifact_type']}", data
                )
    buffer.seek(0)
    headers = {
        "Content-Disposition": f'attachment; filename="comparison-{comparison_id}.zip"'
    }
    return Response(buffer.read(), media_type="application/zip", headers=headers)


@router.post(
    "/comparisons/{comparison_id}/rerun",
    response_model=StartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_comparison(
    request: Request,
    comparison_id: UUID,
    body: ComparisonRerunRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> StartResponse:
    from ..clients.runner import RunnerClient

    admin = _admin(request)
    _get_comparison(admin, comparison_id, user_id)
    runner = RunnerClient(
        request.app.state.settings,
        transport=getattr(request.app.state, "runner_transport", None),
    )
    try:
        new_row, _image, missing = await service.rerun_comparison(
            admin, runner, str(comparison_id), body
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        await runner.aclose()
    await _enqueue(request, UUID(new_row["id"]))
    return StartResponse(
        comparison_id=UUID(new_row["id"]),
        status=ComparisonStatus(new_row["status"]),
        warnings=missing,
    )
