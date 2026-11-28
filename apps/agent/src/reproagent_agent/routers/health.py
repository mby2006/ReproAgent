import time

import httpx
from fastapi import APIRouter, Request, Response
from reproagent_schemas import (
    DependenciesHealthResponse,
    DependencyHealth,
    HealthResponse,
    HealthStatus,
)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Response:
    understanding = getattr(request.app.state, "understanding_graph", None)
    planning = getattr(request.app.state, "planning_graph", None)
    llm_loaded = getattr(request.app.state, "llm", None) is not None
    ready = understanding is not None and planning is not None and llm_loaded
    payload = HealthResponse(
        service="agent",
        status=HealthStatus.OK if ready else HealthStatus.UNAVAILABLE,
        details={
            "understanding_graph_loaded": understanding is not None,
            "planning_graph_loaded": planning is not None,
            "understanding_graph_nodes": 2 if understanding is not None else 0,
            "planning_graph_nodes": 1 if planning is not None else 0,
            "llm_loaded": llm_loaded,
            "llm_configured": bool(request.app.state.settings.qwen_api_key),
        },
    )
    return Response(
        content=payload.model_dump_json(),
        status_code=200 if ready else 503,
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
