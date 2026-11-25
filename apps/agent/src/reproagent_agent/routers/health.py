import time

import httpx
from fastapi import APIRouter, Request, Response
from reproagent_schemas import (
    DependenciesHealthResponse,
    DependencyHealth,
    HealthResponse,
    HealthStatus,
)

from ..graph.build import NODE_ORDER

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Response:
    graph = getattr(request.app.state, "graph", None)
    payload = HealthResponse(
        service="agent",
        status=HealthStatus.OK if graph is not None else HealthStatus.UNAVAILABLE,
        details={
            "graph_loaded": graph is not None,
            "graph_nodes": len(NODE_ORDER),
            "llm_configured": bool(request.app.state.settings.qwen_api_key),
        },
    )
    return Response(
        content=payload.model_dump_json(),
        status_code=200 if graph is not None else 503,
        media_type="application/json",
    )


@router.get("/health/dependencies", response_model=DependenciesHealthResponse)
async def dependencies_health(request: Request) -> DependenciesHealthResponse:
    settings = request.app.state.settings
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
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
        runner = DependencyHealth(
            name="runner",
            status=HealthStatus.UNAVAILABLE,
            detail=type(exc).__name__,
        )
    overall = HealthStatus.OK if runner.status is HealthStatus.OK else HealthStatus.DEGRADED
    return DependenciesHealthResponse(service="agent", status=overall, dependencies=[runner])
