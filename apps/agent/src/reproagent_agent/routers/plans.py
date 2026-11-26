from fastapi import APIRouter, HTTPException, Request, status
from reproagent_schemas import (
    ParsedDocument,
    PlanRequest,
    PlanResponse,
    ReproductionCard,
    SchemaModel,
)

from ..planning import ScalingConfig, UnsupportedPaperError, check_supported

router = APIRouter(prefix="/v1", tags=["planning"])


class SupportCheckRequest(SchemaModel):
    document: ParsedDocument
    card: ReproductionCard | None = None


class SupportCheckResponse(SchemaModel):
    supported: bool
    code: str
    message: str


@router.post("/papers/support-check", response_model=SupportCheckResponse)
async def support_check(body: SupportCheckRequest) -> SupportCheckResponse:
    result = check_supported(body.document, body.card)
    return SupportCheckResponse(
        supported=result.supported, code=result.code, message=result.message
    )


@router.post("/plan", response_model=PlanResponse)
async def plan_experiment(body: PlanRequest, request: Request) -> PlanResponse:
    scaling = ScalingConfig(**body.model_dump(exclude={"card"}, exclude_none=True))
    graph = request.app.state.planning_graph
    try:
        result = await graph.ainvoke({"card": body.card, "scaling": scaling})
    except UnsupportedPaperError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return PlanResponse(plan=result["plan"])
