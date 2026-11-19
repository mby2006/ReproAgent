import importlib.util

from fastapi import APIRouter, Request
from reproagent_schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    torch_available = importlib.util.find_spec("torch") is not None
    return HealthResponse(
        service="runner",
        details={
            "torch_available": torch_available,
            "timeout_seconds": settings.timeout_seconds,
            "work_dir": str(settings.work_dir),
        },
    )
