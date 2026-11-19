from fastapi import APIRouter, HTTPException, Request, status
from reproagent_schemas import PlanRequest, PlanResponse

from ..planning import ScalingConfig, UnsupportedPaperError

router = APIRouter(prefix="/v1", tags=["planning"])


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
