from fastapi import APIRouter
from reproagent_schemas import EvaluateRequest, EvaluateResponse

from ..evaluation import evaluate_run

router = APIRouter(prefix="/v1/evaluate", tags=["evaluation"])


@router.post("", response_model=EvaluateResponse)
async def evaluate(body: EvaluateRequest) -> EvaluateResponse:
    return evaluate_run(body)