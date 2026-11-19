import time

import httpx
from reproagent_schemas import DependenciesHealthResponse, DependencyHealth, HealthStatus

from .config import ApiSettings


async def check_http_dependency(client: httpx.AsyncClient, name: str, url: str) -> DependencyHealth:
    start = time.perf_counter()
    try:
        response = await client.get(url)
        latency = (time.perf_counter() - start) * 1000
        if response.status_code == 200:
            return DependencyHealth(name=name, status=HealthStatus.OK, latency_ms=latency)
        return DependencyHealth(
            name=name,
            status=HealthStatus.UNAVAILABLE,
            latency_ms=latency,
            detail=f"HTTP {response.status_code}",
        )
    except httpx.HTTPError as exc:
        return DependencyHealth(name=name, status=HealthStatus.UNAVAILABLE, detail=str(exc))


async def check_supabase(client: httpx.AsyncClient, settings: ApiSettings) -> DependencyHealth:
    if not settings.supabase_anon_key:
        return DependencyHealth(
            name="supabase", status=HealthStatus.UNAVAILABLE, detail="anon key not configured"
        )
    start = time.perf_counter()
    try:
        response = await client.get(
            f"{settings.supabase_url}/auth/v1/health",
            headers={"apikey": settings.supabase_anon_key},
        )
        latency = (time.perf_counter() - start) * 1000
        if response.status_code == 200:
            return DependencyHealth(name="supabase", status=HealthStatus.OK, latency_ms=latency)
        return DependencyHealth(
            name="supabase",
            status=HealthStatus.UNAVAILABLE,
            latency_ms=latency,
            detail=f"HTTP {response.status_code}",
        )
    except httpx.HTTPError as exc:
        return DependencyHealth(name="supabase", status=HealthStatus.UNAVAILABLE, detail=str(exc))


async def check_runner_via_agent(
    client: httpx.AsyncClient, settings: ApiSettings, agent_status: HealthStatus
) -> DependencyHealth:
    """Runner sits on an isolated network; only the Agent can reach it."""
    if agent_status is not HealthStatus.OK:
        return DependencyHealth(
            name="runner", status=HealthStatus.UNAVAILABLE, detail="agent unavailable"
        )
    try:
        response = await client.get(f"{settings.agent_base_url}/health/dependencies")
        response.raise_for_status()
        payload = DependenciesHealthResponse.model_validate(response.json())
        for dep in payload.dependencies:
            if dep.name == "runner":
                return dep
        return DependencyHealth(
            name="runner", status=HealthStatus.UNAVAILABLE, detail="not reported by agent"
        )
    except (httpx.HTTPError, ValueError) as exc:
        return DependencyHealth(name="runner", status=HealthStatus.UNAVAILABLE, detail=str(exc))


async def check_dependencies(
    settings: ApiSettings, client: httpx.AsyncClient
) -> DependenciesHealthResponse:
    agent = await check_http_dependency(client, "agent", f"{settings.agent_base_url}/health")
    dependencies = [
        agent,
        await check_supabase(client, settings),
        await check_runner_via_agent(client, settings, agent.status),
    ]
    overall = (
        HealthStatus.OK
        if all(dep.status is HealthStatus.OK for dep in dependencies)
        else HealthStatus.DEGRADED
    )
    return DependenciesHealthResponse(service="api", status=overall, dependencies=dependencies)
