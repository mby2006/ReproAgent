from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_schemas import DependencyHealth, HealthStatus


@pytest.fixture
def settings() -> ApiSettings:
    return ApiSettings(
        supabase_url="http://supabase.test",
        supabase_anon_key="anon-test-key",
        supabase_service_role_key="service-test-key",
        agent_base_url="http://agent.test",
        _env_file=None,
    )


@pytest.fixture
def client(settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "reproagent_api.health_checks.check_database",
        lambda settings: DependencyHealth(name="postgres", status=HealthStatus.OK),
    )
    return TestClient(create_app(settings))


def mock_queue_health(monkeypatch):
    monkeypatch.setattr(
        "reproagent_api.health_checks.check_redis",
        AsyncMock(return_value=DependencyHealth(name="redis", status=HealthStatus.OK)),
    )
    monkeypatch.setattr(
        "reproagent_api.health_checks.check_worker",
        AsyncMock(return_value=DependencyHealth(name="worker", status=HealthStatus.OK)),
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "api"
    assert body["status"] == "ok"


def test_dependencies_all_ok(client, settings, monkeypatch):
    mock_queue_health(monkeypatch)
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "agent.test":
            if request.url.path == "/health/dependencies":
                return httpx.Response(
                    200,
                    json={
                        "service": "agent",
                        "status": "ok",
                        "dependencies": [{"name": "runner", "status": "ok", "latency_ms": 1.5}],
                    },
                )
            return httpx.Response(200, json={"service": "agent", "status": "ok"})
        if request.url.path == "/auth/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/storage/v1/bucket":
            return httpx.Response(200, json=[{"id": "papers"}])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "reproagent_api.routers.health.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport),
    )
    response = client.get("/health/dependencies")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {dep["name"] for dep in body["dependencies"]} == {
        "agent", "supabase", "postgres", "storage", "redis", "runner", "worker"
    }


@pytest.mark.parametrize("dependency_status", [200, 503])
def test_dependencies_ignore_host_proxy_settings(client, monkeypatch, dependency_status):
    mock_queue_health(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,[::1]")

    async def get(self, url, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(
            dependency_status,
            request=request,
            json={
                "service": "agent",
                "status": "ok",
                "dependencies": [{"name": "runner", "status": "ok"}],
            },
        )

    # Keep the real constructor so NO_PROXY parsing is exercised.
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    response = client.get("/health/dependencies")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == ("ok" if dependency_status == 200 else "degraded")
    assert {dep["name"] for dep in body["dependencies"]} == {
        "agent", "supabase", "postgres", "storage", "redis", "runner", "worker"
    }
    remote = {"agent", "supabase", "runner", "storage"}
    assert all(
        dep["status"] == ("ok" if dependency_status == 200 else "unavailable")
        for dep in body["dependencies"]
        if dep["name"] in remote
    )


def test_dependencies_degraded_when_agent_down(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "agent.test":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "reproagent_api.routers.health.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport),
    )
    body = client.get("/health/dependencies").json()
    assert body["status"] == "degraded"
    agent_dep = next(dep for dep in body["dependencies"] if dep["name"] == "agent")
    assert agent_dep["status"] == "unavailable"
