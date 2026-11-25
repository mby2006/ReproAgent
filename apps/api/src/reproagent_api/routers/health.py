import json
import secrets
import time

import httpx
from fastapi import APIRouter, Request, Response
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
        return await check_dependencies(settings, client, request.app.state.arq)


@router.get("/ready")
async def readiness(request: Request) -> Response:
    snapshot = getattr(request.app.state, "health_snapshot", None)
    fresh = snapshot is not None and time.monotonic() - snapshot["checked_at"] <= 45
    status = snapshot["status"] if fresh else "unavailable"
    body = {
        "service": "api",
        "status": status,
        "age_seconds": round(time.monotonic() - snapshot["checked_at"], 1) if fresh else None,
    }
    return Response(
        content=json.dumps(body),
        status_code=200 if status == "ok" else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    token = request.app.state.settings.ops_token
    if not token:
        return Response(status_code=404)
    supplied = request.headers.get("X-Ops-Token", "")
    if not secrets.compare_digest(supplied, token):
        return Response(status_code=401)
    return Response(content=request.app.state.metrics.render(), media_type="text/plain")
