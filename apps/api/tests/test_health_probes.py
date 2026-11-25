from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from reproagent_api.config import ApiSettings
from reproagent_api.health_checks import (
    check_database,
    check_http_dependency,
    check_redis,
    check_storage,
    check_worker,
)
from reproagent_api.worker import publish_worker_heartbeat
from reproagent_schemas import HealthStatus


@pytest.fixture
def settings():
    return ApiSettings(
        supabase_url="http://supabase.test",
        supabase_anon_key="anon-test-key",
        supabase_service_role_key="service-secret",
        supabase_db_url="postgresql://user:password@db.test/postgres",
        _env_file=None,
    )


def test_database_probe_executes_real_query_and_redacts_failures(settings, monkeypatch):
    class Connection:
        def execute(self, query):
            assert query == "select 1"
            return SimpleNamespace(fetchone=lambda: (1,))

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "reproagent_api.health_checks.psycopg.connect", lambda *a, **k: Connection()
    )
    result = check_database(settings)
    assert result.status is HealthStatus.OK

    def fail(*args, **kwargs):
        raise RuntimeError("password=password host=db.test")

    monkeypatch.setattr("reproagent_api.health_checks.psycopg.connect", fail)
    result = check_database(settings)
    assert result.status is HealthStatus.UNAVAILABLE
    assert "password" not in (result.detail or "")


@pytest.mark.asyncio
async def test_storage_probe_uses_service_auth_and_checks_bucket_api(settings):
    def handler(request):
        assert request.url.path == "/storage/v1/bucket"
        assert request.headers["authorization"] == "Bearer service-secret"
        assert request.headers["apikey"] == "service-secret"
        return httpx.Response(200, json=[{"id": "papers"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_storage(client, settings)
    assert result.status is HealthStatus.OK


class FakeRedis:
    def __init__(self, heartbeat=None, error=None):
        self.heartbeat = heartbeat
        self.error = error

    async def ping(self):
        if self.error:
            raise self.error
        return True

    async def get(self, key):
        assert key == "reproagent:worker:heartbeat"
        return self.heartbeat


@pytest.mark.asyncio
async def test_http_probe_failures_are_redacted():
    def handler(request):
        raise httpx.ConnectError(
            "token=secret-value", request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_http_dependency(
            client, "agent", "http://agent.test/health?token=secret-value"
        )
    assert result.status is HealthStatus.UNAVAILABLE
    assert result.detail == "ConnectError"
    assert "secret-value" not in (result.detail or "")


@pytest.mark.asyncio
async def test_redis_and_fresh_worker_heartbeat_are_healthy():
    redis = FakeRedis(datetime.now(UTC).isoformat().encode())
    assert (await check_redis(redis)).status is HealthStatus.OK
    assert (await check_worker(redis)).status is HealthStatus.OK


@pytest.mark.asyncio
async def test_missing_stale_or_invalid_worker_heartbeat_is_unavailable():
    for value in (None, (datetime.now(UTC) - timedelta(minutes=5)).isoformat().encode(), b"bad"):
        result = await check_worker(FakeRedis(value))
        assert result.status is HealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_future_worker_heartbeat_is_unavailable():
    value = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().encode()
    result = await check_worker(FakeRedis(value))
    assert result.status is HealthStatus.UNAVAILABLE
    assert result.detail == "ValueError"


@pytest.mark.asyncio
async def test_redis_failure_is_redacted():
    result = await check_redis(FakeRedis(error=RuntimeError("redis://user:password@host")))
    assert result.status is HealthStatus.UNAVAILABLE
    assert "password" not in (result.detail or "")


@pytest.mark.asyncio
async def test_worker_publishes_heartbeat_without_exposing_secrets():
    class Writer(FakeRedis):
        async def set(self, key, value, ex=None):
            self.key = key
            self.value = value
            self.ex = ex

    redis = Writer()
    await publish_worker_heartbeat({"redis": redis})
    assert redis.key == "reproagent:worker:heartbeat"
    value = redis.value.decode() if isinstance(redis.value, bytes) else redis.value
    assert datetime.fromisoformat(value).tzinfo is not None
    assert redis.ex == 60
