"""Agent endpoints for paired optimizer comparisons (deterministic, no LLM)."""

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError
from reproagent_schemas import (
    CandidateRequest,
    CandidateResponse,
    ComparisonPlanRequest,
    ComparisonPlanResponse,
    ComparisonReportRequest,
    ComparisonReportResponse,
    VerifyPairRequest,
    VerifyPairResponse,
)

from ..comparison import (
    UnsupportedComparisonError,
    build_comparison_plan,
    build_comparison_report,
    find_candidates,
    verify_pairs,
)

router = APIRouter(prefix="/v1/comparisons", tags=["comparisons"])


@router.post("/candidates", response_model=CandidateResponse)
async def candidates(body: CandidateRequest) -> CandidateResponse:
    return find_candidates(body)


@router.post("/plan", response_model=ComparisonPlanResponse)
async def plan(body: ComparisonPlanRequest) -> ComparisonPlanResponse:
    try:
        return build_comparison_plan(body)
    except UnsupportedComparisonError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": str(exc), "missing": exc.gaps},
        ) from exc
    except ValidationError as exc:
        # Invalid combinations (duplicate seeds, identical optimizers, out-of-range
        # values) are user input errors, never a 500.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "对照计划参数无效",
                "missing": [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ],
            },
        ) from exc


@router.post("/verify", response_model=VerifyPairResponse)
async def verify(body: VerifyPairRequest) -> VerifyPairResponse:
    return VerifyPairResponse(
        outcome=verify_pairs(body.plan, body.observations, body.requested_pairs)
    )


@router.post("/report", response_model=ComparisonReportResponse)
async def report(body: ComparisonReportRequest) -> ComparisonReportResponse:
    return build_comparison_report(body)
