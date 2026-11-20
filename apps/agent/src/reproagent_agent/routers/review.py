"""Generate review advice only; adoption and all persistence belong to the API."""

from fastapi import APIRouter, HTTPException, Request, status
from reproagent_schemas import ExplainResponse, RecommendResponse, ReviewRequest

from ..review import InvalidReviewRequest, ReviewError

router = APIRouter(prefix="/v1", tags=["review"])


@router.post("/explain", response_model=ExplainResponse)
async def explain(body: ReviewRequest, request: Request) -> ExplainResponse:
    try:
        return await request.app.state.review_service.explain(body)
    except InvalidReviewRequest as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(body: ReviewRequest, request: Request) -> RecommendResponse:
    try:
        return await request.app.state.review_service.recommend(body)
    except InvalidReviewRequest as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
