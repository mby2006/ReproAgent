import time

import httpx
from fastapi import APIRouter, Request
from reproagent_schemas import (
    DependenciesHealthResponse,
    DependencyHealth,
    HealthResponse,
    HealthStatus,
)

from ..graph.build import NODE_ORDER

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    graph = getattr(request.app.state, "graph", None)
    return HealthResponse(
        service="agent",
        status=HealthStatus.OK if graph is not None else HealthStatus.UNAVAILABLE,
        details={
            "graph_loaded": graph is not None,
            "graph_nodes": len(NODE_ORDER),
            "llm_configured": bool(request.app.state.settings.qwen_api_key),
        },
    )


@router.get("/health/dependencies", response_model=DependenciesHealthResponse)
async def dependencies_health(request: Request) -> DependenciesHealthResponse:
    settings = request.app.state.settings
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.runner_base_url}/health")
        latency = (time.perf_counter() - start) * 1000
        if response.status_code == 200:
            runner = DependencyHealth(name="runner", status=HealthStatus.OK, latency_ms=latency)
        else:
            runner = DependencyHealth(
                name="runner",
                status=HealthStatus.UNAVAILABLE,
                latency_ms=latency,
                detail=f"HTTP {response.status_code}",
            )
    except httpx.HTTPError as exc:
        runner = DependencyHealth(name="runner", status=HealthStatus.UNAVAILABLE, detail=str(exc))
    overall = HealthStatus.OK if runner.status is HealthStatus.OK else HealthStatus.DEGRADED
    return DependenciesHealthResponse(service="agent", status=overall, dependencies=[runner])
