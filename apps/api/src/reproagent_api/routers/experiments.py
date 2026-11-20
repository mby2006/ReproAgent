"""Experiment orchestration: create experiments, generate/version/approve plans.

Planning is deterministic and fast, so generation runs synchronously in-request
(api -> agent /v1/plan). Ownership flows experiments.project_id -> projects.user_id.
Every config change inserts a NEW plan version; the DB trigger forbids overwriting
old config, and approval only flips approved_at after the confirmation gate passes.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field
from reproagent_schemas import (
    AdoptRecommendationsRequest,
    ConfirmationSource,
    DelegateReviewRequest,
    ExperimentPlan,
    ExplainFieldsRequest,
    ExplainResponse,
    PlanChange,
    PlanEditRequest,
    PlanOrigin,
    PlanRequest,
    PlanVersion,
    RecommendResponse,
    ReproductionCard,
    ReviewMode,
    ReviewModeRequest,
    ReviewRequest,
    SchemaModel,
    TierOverrideRequest,
)

from ..auth import get_current_user_id
from ..clients.agent import AgentClient
from ..clients.supabase import make_service_client
from ..services import PlanEditError, apply_plan_edits, insert_versioned
from ..services.plan_edits import apply_delegated_review, apply_tier_override
from ..services.review import generate_review

router = APIRouter(prefix="/v1", tags=["experiments"])

PLAN_COLUMNS = (
    "id,experiment_id,version,origin,parent_version_id,config_json,budget_json,"
    "change_reason,related_error,related_run_id,changes_json,approved_at,created_at"
)
EXPERIMENT_COLUMNS = "id,paper_id,spec_id,name,status,created_at"


class ExperimentCreate(SchemaModel):
    name: str | None = Field(default=None, max_length=200)
    spec_version: int | None = Field(default=None, ge=1)


class ExperimentSummary(SchemaModel):
    id: UUID
    paper_id: UUID
    name: str
    status: str
    created_at: datetime


class ExperimentDetail(SchemaModel):
    id: UUID
    paper_id: UUID
    spec_id: UUID | None = None
    name: str
    status: str
    created_at: datetime
    plan: PlanVersion | None = None


class PlanVersionMeta(SchemaModel):
    id: UUID
    version: int
    origin: PlanOrigin
    parent_version_id: UUID | None = None
    change_reason: str | None = None
    approved_at: datetime | None = None
    created_at: datetime


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


def _to_plan_version(row: dict) -> PlanVersion:
    return PlanVersion(
        id=row["id"],
        experiment_id=row["experiment_id"],
        version=row["version"],
        origin=row["origin"],
        parent_version_id=row["parent_version_id"],
        plan=row["config_json"],
        change_reason=row["change_reason"],
        changes=row["changes_json"],
        related_error=row["related_error"],
        related_run_id=row["related_run_id"],
        approved_at=row["approved_at"],
        created_at=row["created_at"],
    )


def _latest_plan(admin, experiment_id: str) -> dict | None:
    rows = (
        admin.table("experiment_plans")
        .select(PLAN_COLUMNS)
        .eq("experiment_id", experiment_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _insert_plan(admin, experiment_id: str, version: int, plan: ExperimentPlan, **meta) -> dict:
    row = {
        "experiment_id": experiment_id,
        "version": version,
        "config_json": plan.model_dump(mode="json"),
        "budget_json": plan.runtime_budget.model_dump(mode="json"),
    }
    row.update(meta)
    return admin.table("experiment_plans").insert(row).execute().data[0]


@router.post(
    "/papers/{paper_id}/experiments",
    response_model=ExperimentDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment(
    request: Request,
    paper_id: UUID,
    body: ExperimentCreate,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> ExperimentDetail:
    admin = _admin(request)
    paper = _get_paper(admin, paper_id, user_id)

    query = admin.table("reproduction_specs").select("id,spec_json").eq("paper_id", str(paper_id))
    if body.spec_version is not None:
        specs = query.eq("version", body.spec_version).execute().data
    else:
        specs = query.order("version", desc=True).limit(1).execute().data
    if not specs:
        raise HTTPException(status.HTTP_409_CONFLICT, "论文尚无 Reproduction Card，请先完成解析")
    spec = specs[0]
    card = ReproductionCard.model_validate(spec["spec_json"])
    if card.review_mode is ReviewMode.DELEGATED and card.delegated_by != user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "理解卡托管授权不属于当前用户")

    agent = _agent(request)
    try:
        response = await agent.plan(PlanRequest(card=card))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Agent unavailable: {exc}") from exc
    finally:
        await agent.aclose()
    plan = response.plan
    records = []
    approved_at = None
    if card.review_mode is ReviewMode.DELEGATED:
        context = ReviewRequest(card=card, plan=plan, scope=str(user_id))
        advice = await generate_review(request, context, recommend=True)
        try:
            plan, records = apply_delegated_review(
                plan, advice.recommendations, actor=user_id, delegated_at=card.delegated_at,
            )
        except PlanEditError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "托管推荐不可用，请重试或使用专家模式",
            ) from exc
        if not plan.approval_ready(user_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "托管审核尚未完成，不能批准")
        approved_at = datetime.now(UTC).isoformat()

    experiment_id = uuid4()
    name = body.name or f"{paper['title']} 复现"
    admin.table("experiments").insert(
        {
            "id": str(experiment_id),
            "project_id": paper["project_id"],
            "paper_id": str(paper_id),
            "spec_id": spec["id"],
            "name": name[:200],
            "status": "approved" if approved_at else "planning",
        }
    ).execute()
    plan_row = _insert_plan(
        admin, str(experiment_id), 1, plan, origin=PlanOrigin.GENERATED.value,
        approved_at=approved_at, changes_json=[r.model_dump(mode="json") for r in records],
    )
    return ExperimentDetail(
        id=experiment_id,
        paper_id=paper_id,
        spec_id=spec["id"],
        name=name[:200],
        status="approved" if approved_at else "planning",
        created_at=datetime.now(UTC),
        plan=_to_plan_version(plan_row),
    )


@router.get("/papers/{paper_id}/experiments", response_model=list[ExperimentSummary])
async def list_experiments(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[ExperimentSummary]:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    rows = (
        admin.table("experiments")
        .select(EXPERIMENT_COLUMNS)
        .eq("paper_id", str(paper_id))
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return [ExperimentSummary.model_validate(row) for row in rows]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(
    request: Request, experiment_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> ExperimentDetail:
    admin = _admin(request)
    experiment = _get_experiment(admin, experiment_id, user_id)
    plan_row = _latest_plan(admin, str(experiment_id))
    return ExperimentDetail(
        id=experiment["id"],
        paper_id=experiment["paper_id"],
        spec_id=experiment["spec_id"],
        name=experiment["name"],
        status=experiment["status"],
        created_at=experiment["created_at"],
        plan=_to_plan_version(plan_row) if plan_row else None,
    )


@router.get("/experiments/{experiment_id}/plans", response_model=list[PlanVersionMeta])
async def list_plans(
    request: Request, experiment_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[PlanVersionMeta]:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    rows = (
        admin.table("experiment_plans")
        .select("id,version,origin,parent_version_id,change_reason,approved_at,created_at")
        .eq("experiment_id", str(experiment_id))
        .order("version")
        .execute()
        .data
    )
    return [PlanVersionMeta.model_validate(row) for row in rows]


@router.get("/experiments/{experiment_id}/plans/{version}", response_model=PlanVersion)
async def get_plan(
    request: Request,
    experiment_id: UUID,
    version: int,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanVersion:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    rows = (
        admin.table("experiment_plans")
        .select(PLAN_COLUMNS)
        .eq("experiment_id", str(experiment_id))
        .eq("version", version)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan version not found")
    return _to_plan_version(rows[0])


@router.post(
    "/experiments/{experiment_id}/plans",
    response_model=PlanVersion,
    status_code=status.HTTP_201_CREATED,
)
async def edit_plan(
    request: Request,
    experiment_id: UUID,
    body: PlanEditRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanVersion:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    base_rows = (
        admin.table("experiment_plans")
        .select(PLAN_COLUMNS)
        .eq("experiment_id", str(experiment_id))
        .eq("version", body.base_version)
        .execute()
        .data
    )
    if not base_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Base plan version not found")
    base = base_rows[0]
    try:
        new_plan, records = apply_plan_edits(
            ExperimentPlan.model_validate(base["config_json"]), body, actor=user_id
        )
    except PlanEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    def next_version() -> int:
        latest = _latest_plan(admin, str(experiment_id))
        return latest["version"] + 1 if latest else 1

    def insert(v: int) -> dict:
        return _insert_plan(
            admin,
            str(experiment_id),
            v,
            new_plan,
            origin=body.origin.value,
            parent_version_id=base["id"],
            change_reason=body.change_reason,
            related_error=body.related_error,
            related_run_id=str(body.related_run_id) if body.related_run_id else None,
            changes_json=[r.model_dump(mode="json") for r in records],
        )

    plan_row = insert_versioned(next_version, insert)
    return _to_plan_version(plan_row)


@router.post(
    "/experiments/{experiment_id}/plans/{version}/tier",
    response_model=PlanVersion,
    status_code=status.HTTP_201_CREATED,
)
async def override_plan_tier(
    request: Request,
    experiment_id: UUID,
    version: int,
    body: TierOverrideRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanVersion:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    base_rows = (
        admin.table("experiment_plans")
        .select(PLAN_COLUMNS)
        .eq("experiment_id", str(experiment_id))
        .eq("version", version)
        .execute()
        .data
    )
    if not base_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Base plan version not found")
    base = base_rows[0]
    try:
        new_plan, records = apply_tier_override(
            ExperimentPlan.model_validate(base["config_json"]),
            body.field_path, body.risk_tier, actor=user_id,
        )
    except PlanEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    def next_version() -> int:
        latest = _latest_plan(admin, str(experiment_id))
        return latest["version"] + 1 if latest else 1

    plan_row = insert_versioned(
        next_version,
        lambda v: _insert_plan(
            admin, str(experiment_id), v, new_plan,
            origin=PlanOrigin.USER_EDIT.value,
            parent_version_id=base["id"],
            change_reason=f"人工调整假设分级：{body.field_path} → {body.risk_tier.value}",
            changes_json=[r.model_dump(mode="json") for r in records],
        ),
    )
    return _to_plan_version(plan_row)


def _review_context(admin, experiment_id: UUID, version: int, user_id: UUID):
    experiment = _get_experiment(admin, experiment_id, user_id)
    rows = (
        admin.table("experiment_plans").select(PLAN_COLUMNS)
        .eq("experiment_id", str(experiment_id)).eq("version", version).execute().data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan version not found")
    specs = (
        admin.table("reproduction_specs").select("spec_json")
        .eq("id", experiment["spec_id"]).eq("paper_id", experiment["paper_id"]).execute().data
    ) if experiment["spec_id"] else []
    if not specs:
        raise HTTPException(status.HTTP_409_CONFLICT, "计划关联的理解卡不可用，请使用专家模式")
    row = rows[0]
    return row, ReviewRequest(
        card=ReproductionCard.model_validate(specs[0]["spec_json"]),
        plan=ExperimentPlan.model_validate(row["config_json"]), scope=str(user_id),
    )


def _next_review_version(admin, experiment_id: UUID, base_id: str) -> int:
    latest = _latest_plan(admin, str(experiment_id))
    if latest is None or latest["id"] != base_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "计划版本已变化，请刷新后重新审核")
    return latest["version"] + 1


@router.post(
    "/experiments/{experiment_id}/plans/{version}/explain", response_model=ExplainResponse,
)
async def explain_plan(
    request: Request, experiment_id: UUID, version: int, body: ExplainFieldsRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> ExplainResponse:
    _, context = _review_context(_admin(request), experiment_id, version, user_id)
    context.field_paths = body.field_paths
    return await generate_review(request, context)


@router.post(
    "/experiments/{experiment_id}/plans/{version}/recommend", response_model=RecommendResponse,
)
async def recommend_plan(
    request: Request, experiment_id: UUID, version: int,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> RecommendResponse:
    _, context = _review_context(_admin(request), experiment_id, version, user_id)
    return await generate_review(request, context, recommend=True)


@router.post(
    "/experiments/{experiment_id}/plans/{version}/adopt", response_model=PlanVersion,
    status_code=status.HTTP_201_CREATED,
)
async def adopt_recommendations(
    request: Request, experiment_id: UUID, version: int, body: AdoptRecommendationsRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanVersion:
    admin = _admin(request)
    base, context = _review_context(admin, experiment_id, version, user_id)
    _next_review_version(admin, experiment_id, base["id"])
    if context.context_hash() != body.context_hash:
        raise HTTPException(status.HTTP_409_CONFLICT, "推荐上下文已变化，请重新获取推荐")
    response = await generate_review(request, context, recommend=True)
    by_id = {item.id: item for item in response.recommendations}
    if set(body.recommendation_ids) - by_id.keys():
        raise HTTPException(status.HTTP_409_CONFLICT, "推荐已过期，请重新获取并确认")
    selected = [by_id[key] for key in body.recommendation_ids]
    edit = PlanEditRequest(
        base_version=version, review_mode=body.review_mode,
        changes=[PlanChange(field_path=r.field_path, value=r.suggested_value) for r in selected],
        change_reason=f"人工采纳 {len(selected)} 项 AI 推荐",
    )
    try:
        plan, records = apply_plan_edits(
            context.plan, edit, confirmed_by=ConfirmationSource.AI_ASSISTED, actor=user_id,
            recommendations={r.field_path: r for r in selected},
            preselection_threshold=response.selection_threshold,
        )
    except PlanEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    row = insert_versioned(
        lambda: _next_review_version(admin, experiment_id, base["id"]),
        lambda v: _insert_plan(
            admin, str(experiment_id), v, plan, origin=PlanOrigin.USER_EDIT.value,
            parent_version_id=base["id"], change_reason=edit.change_reason,
            changes_json=[record.model_dump(mode="json") for record in records],
        ),
    )
    return _to_plan_version(row)


@router.post(
    "/experiments/{experiment_id}/plans/{version}/delegate", response_model=PlanVersion,
    status_code=status.HTTP_201_CREATED,
)
async def delegate_plan(
    request: Request, experiment_id: UUID, version: int, body: DelegateReviewRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> PlanVersion:
    admin = _admin(request)
    base, context = _review_context(admin, experiment_id, version, user_id)
    if body.base_version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, "授权版本不匹配，请刷新后重试")
    _next_review_version(admin, experiment_id, base["id"])
    response = await generate_review(request, context, recommend=True)
    try:
        plan, records = apply_delegated_review(
            context.plan, response.recommendations, actor=user_id,
        )
    except PlanEditError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "托管推荐不可用，请重试或使用专家模式",
        ) from exc
    if not plan.approval_ready(user_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "托管审核尚未完成，不能批准")
    row = insert_versioned(
        lambda: _next_review_version(admin, experiment_id, base["id"]),
        lambda v: _insert_plan(
            admin, str(experiment_id), v, plan, origin=PlanOrigin.USER_EDIT.value,
            parent_version_id=base["id"], change_reason="用户显式授权托管审核并批准",
            changes_json=[record.model_dump(mode="json") for record in records],
            approved_at=datetime.now(UTC).isoformat(),
        ),
    )
    admin.table("experiments").update({"status": "approved"}).eq(
        "id", str(experiment_id),
    ).execute()
    return _to_plan_version(row)


@router.post(
    "/experiments/{experiment_id}/plans/{version}/approve",
    response_model=PlanVersion,
)
async def approve_plan(
    request: Request,
    experiment_id: UUID,
    version: int,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    body: ReviewModeRequest | None = None,
) -> PlanVersion:
    admin = _admin(request)
    _get_experiment(admin, experiment_id, user_id)
    rows = (
        admin.table("experiment_plans")
        .select(PLAN_COLUMNS)
        .eq("experiment_id", str(experiment_id))
        .eq("version", version)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan version not found")
    row = rows[0]
    plan = ExperimentPlan.model_validate(row["config_json"])
    mode_records = []
    if body is not None and body.review_mode != plan.review_mode:
        plan, mode_records = apply_plan_edits(
            plan, PlanEditRequest(
                base_version=version, review_mode=body.review_mode,
                change_reason=f"以 {body.review_mode.value} 模式批准计划",
            ), actor=user_id,
        )
    if not plan.approval_ready(user_id):
        paths = ", ".join(a.field_path for a in plan.unconfirmed_critical())
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"存在未确认的关键假设或托管授权无效，请先审核：{paths}",
        )
    if mode_records:
        base_id = row["id"]
        row = insert_versioned(
            lambda: _next_review_version(admin, experiment_id, base_id),
            lambda v: _insert_plan(
                admin, str(experiment_id), v, plan, origin=PlanOrigin.USER_EDIT.value,
                parent_version_id=base_id, change_reason="批准时记录审核模式",
                changes_json=[record.model_dump(mode="json") for record in mode_records],
            ),
        )
    if row["approved_at"] is None:
        now = datetime.now(UTC).isoformat()
        admin.table("experiment_plans").update({"approved_at": now}).eq("id", row["id"]).execute()
        admin.table("experiments").update({"status": "approved"}).eq(
            "id", str(experiment_id)
        ).execute()
        row["approved_at"] = now
    return _to_plan_version(row)
