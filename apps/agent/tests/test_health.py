import httpx
import pytest
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.main import create_app


@pytest.fixture
def client():
    settings = AgentSettings(runner_base_url="http://runner.test", _env_file=None)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_reports_graph_loaded(client):
    body = client.get("/health").json()
    assert body["service"] == "agent"
    assert body["status"] == "ok"
    assert body["details"]["graph_loaded"] is True
    assert body["details"]["graph_nodes"] == 7


def test_dependencies_runner_ok(client, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "reproagent_agent.routers.health.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport),
    )
    body = client.get("/health/dependencies").json()
    assert body["status"] == "ok"
    assert body["dependencies"][0]["name"] == "runner"


def test_dependencies_runner_down(client, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "reproagent_agent.routers.health.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    body = client.get("/health/dependencies").json()
    assert body["status"] == "degraded"
    assert body["dependencies"][0]["status"] == "unavailable"
