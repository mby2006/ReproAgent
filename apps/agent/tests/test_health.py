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


def test_health_reports_active_graph_readiness_and_llm_configuration(client):
    body = client.get("/health").json()
    assert body["service"] == "agent"
    assert body["status"] == "ok"
    assert body["details"] == {
        "understanding_graph_loaded": True,
        "planning_graph_loaded": True,
        "understanding_graph_nodes": 2,
        "planning_graph_nodes": 1,
        "llm_loaded": True,
        "llm_configured": False,
    }


@pytest.mark.parametrize("graph_name", ["understanding_graph", "planning_graph"])
def test_health_is_unavailable_without_each_active_graph(graph_name):
    app = create_app(AgentSettings(runner_base_url="http://runner.test", _env_file=None))
    setattr(app.state, graph_name, None)
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_health_is_unavailable_without_llm_adapter():
    app = create_app(AgentSettings(runner_base_url="http://runner.test", _env_file=None))
    app.state.llm = None
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


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


@pytest.mark.parametrize("dependency_status", [200, 503])
def test_dependencies_ignore_host_proxy_settings(client, monkeypatch, dependency_status):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,[::1]")

    async def get(self, url, **kwargs):
        assert url == "http://runner.test/health"
        return httpx.Response(dependency_status, request=httpx.Request("GET", url))

    # Keep the real constructor so NO_PROXY parsing is exercised.
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    response = client.get("/health/dependencies")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == ("ok" if dependency_status == 200 else "degraded")
    assert body["dependencies"][0]["name"] == "runner"
    assert body["dependencies"][0]["status"] == (
        "ok" if dependency_status == 200 else "unavailable"
    )


def test_dependencies_runner_down(client, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("token=secret-value", request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "reproagent_agent.routers.health.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    body = client.get("/health/dependencies").json()
    assert body["status"] == "degraded"
    assert body["dependencies"][0]["name"] == "runner"
    assert body["dependencies"][0]["status"] == "unavailable"
    assert body["dependencies"][0]["detail"] == "ConnectError"
