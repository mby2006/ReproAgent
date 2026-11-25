import asyncio
import time
from datetime import UTC, datetime

import httpx
import psycopg
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
        return DependencyHealth(name=name, status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc))


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
        return DependencyHealth(
            name="supabase", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


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
        return DependencyHealth(
            name="runner", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


def _safe_error(exc):
    return type(exc).__name__


def check_database(settings: ApiSettings) -> DependencyHealth:
    start = time.perf_counter()
    try:
        with psycopg.connect(
            settings.supabase_db_url,
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as connection:
            connection.execute("select 1").fetchone()
        return DependencyHealth(
            name="postgres",
            status=HealthStatus.OK,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception as exc:
        return DependencyHealth(
            name="postgres", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


async def check_storage(client: httpx.AsyncClient, settings: ApiSettings) -> DependencyHealth:
    if not settings.supabase_service_role_key:
        return DependencyHealth(
            name="storage", status=HealthStatus.UNAVAILABLE, detail="service key not configured"
        )
    start = time.perf_counter()
    try:
        response = await client.get(
            f"{settings.supabase_url}/storage/v1/bucket",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
        )
        latency = (time.perf_counter() - start) * 1000
        status = HealthStatus.OK if response.status_code == 200 else HealthStatus.UNAVAILABLE
        return DependencyHealth(
            name="storage",
            status=status,
            latency_ms=latency,
            detail=None if status is HealthStatus.OK else f"HTTP {response.status_code}",
        )
    except httpx.HTTPError as exc:
        return DependencyHealth(
            name="storage", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


async def check_redis(redis) -> DependencyHealth:
    if redis is None:
        return DependencyHealth(name="redis", status=HealthStatus.UNAVAILABLE, detail="no pool")
    start = time.perf_counter()
    try:
        await redis.ping()
        return DependencyHealth(
            name="redis",
            status=HealthStatus.OK,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception as exc:
        return DependencyHealth(
            name="redis", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


async def check_worker(redis) -> DependencyHealth:
    if redis is None:
        return DependencyHealth(name="worker", status=HealthStatus.UNAVAILABLE, detail="no pool")
    try:
        raw = await redis.get("reproagent:worker:heartbeat")
        if raw is None:
            raise ValueError("heartbeat missing")
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        heartbeat = datetime.fromisoformat(value)
        age = (datetime.now(UTC) - heartbeat).total_seconds()
        if age < -45 or age > 45:
            raise ValueError("heartbeat stale")
        return DependencyHealth(name="worker", status=HealthStatus.OK, latency_ms=age * 1000)
    except Exception as exc:
        return DependencyHealth(
            name="worker", status=HealthStatus.UNAVAILABLE, detail=_safe_error(exc)
        )


async def check_dependencies(
    settings: ApiSettings, client: httpx.AsyncClient, redis=None
) -> DependenciesHealthResponse:
    agent = await check_http_dependency(client, "agent", f"{settings.agent_base_url}/health")
    supabase, postgres, storage, redis_health, runner, worker = await asyncio.gather(
        check_supabase(client, settings),
        asyncio.to_thread(check_database, settings),
        check_storage(client, settings),
        check_redis(redis),
        check_runner_via_agent(client, settings, agent.status),
        check_worker(redis),
    )
    dependencies = [agent, supabase, postgres, storage, redis_health, runner, worker]
    overall = (
        HealthStatus.OK
        if all(dep.status is HealthStatus.OK for dep in dependencies)
        else HealthStatus.DEGRADED
    )
    return DependenciesHealthResponse(service="api", status=overall, dependencies=dependencies)
