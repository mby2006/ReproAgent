import httpx
from fastapi import APIRouter, Request
from reproagent_schemas import DependenciesHealthResponse, HealthResponse

from ..health_checks import check_dependencies

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(service="api", details={"environment": settings.environment})


@router.get("/health/dependencies", response_model=DependenciesHealthResponse)
async def dependencies_health(request: Request) -> DependenciesHealthResponse:
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        return await check_dependencies(settings, client)
