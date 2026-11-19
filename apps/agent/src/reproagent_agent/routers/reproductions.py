from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from reproagent_schemas import JobAccepted, ReproductionRequest

router = APIRouter(prefix="/v1", tags=["reproductions"])


@router.post("/reproductions", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_reproduction(request: ReproductionRequest) -> JobAccepted:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Agent workflow is not implemented in phase 1",
    )


@router.get("/jobs/{job_id}", response_model=JobAccepted)
async def get_job(job_id: UUID) -> JobAccepted:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Job tracking is not implemented in phase 1",
    )
