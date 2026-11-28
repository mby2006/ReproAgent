import json
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.main import create_app as create_agent_app
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app as create_api_app


def route_paths(app) -> set[str]:
    return set(app.openapi()["paths"])


def test_agent_has_no_placeholder_or_501_routes():
    paths = route_paths(create_agent_app(AgentSettings(_env_file=None)))
    assert not any(path.startswith("/v1/reproductions") for path in paths)
    assert not any(path.startswith("/v1/jobs") for path in paths)
    assert "/health" in paths


def test_api_has_no_placeholder_or_501_routes():
    paths = route_paths(create_api_app(ApiSettings(_env_file=None)))
    assert not any(path.startswith("/v1/reproductions") for path in paths)
    assert not any(path.startswith("/v1/jobs") for path in paths)
    assert "/health" in paths


def test_agent_keeps_active_understanding_and_planning_routes():
    paths = route_paths(create_agent_app(AgentSettings(_env_file=None)))
    assert {"/v1/understand", "/v1/plan", "/v1/evaluate", "/health/dependencies"} <= paths


def test_api_keeps_real_run_evidence_timing_and_workflow_routes():
    paths = route_paths(create_api_app(ApiSettings(_env_file=None)))
    assert {
        "/v1/papers",
        "/v1/experiments/{experiment_id}/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/timings",
        "/v1/runs/{run_id}/evidence-index",
        "/v1/runs/{run_id}/report",
        "/v1/workflows/{workflow_id}/traces",
    } <= paths


def test_agent_lifespan_initializes_only_active_graphs():
    app = create_agent_app(AgentSettings(qwen_api_key="configured-test-key", _env_file=None))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["details"] == {
        "understanding_graph_loaded": True,
        "planning_graph_loaded": True,
        "understanding_graph_nodes": 2,
        "planning_graph_nodes": 1,
        "llm_loaded": True,
        "llm_configured": True,
    }
    assert not hasattr(app.state, "graph")
    assert app.state.understanding_graph is not None
    assert app.state.planning_graph is not None


def test_agent_fresh_process_does_not_import_runner_or_full_graph_symbols():
    code = """
import json
import sys
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.graph import build
from reproagent_agent.main import create_app
app = create_app(AgentSettings(_env_file=None))
with TestClient(app):
    pass
print(json.dumps({
    "runner_imported": "reproagent_agent.clients.runner" in sys.modules,
    "reproductions_imported": "reproagent_agent.routers.reproductions" in sys.modules,
    "full_graph_builder": hasattr(build, "build_graph"),
    "legacy_node_order": hasattr(build, "NODE_ORDER"),
    "legacy_llm_resolver": hasattr(build, "_resolve_llm"),
    "legacy_app_graph": hasattr(app.state, "graph"),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "runner_imported": False,
        "reproductions_imported": False,
        "full_graph_builder": False,
        "legacy_node_order": False,
        "legacy_llm_resolver": False,
        "legacy_app_graph": False,
    }


def test_smoke_checks_active_health_and_deterministic_plan(monkeypatch):
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "scripts" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location("task14_smoke_test", path)
    assert spec is not None and spec.loader is not None
    smoke_test = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke_test)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            if url == f"{smoke_test.WEB_URL}":
                return httpx.Response(200)
            if url == f"{smoke_test.API_URL}/health":
                return httpx.Response(200, json={"status": "ok"})
            if url == f"{smoke_test.AGENT_URL}/health":
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "details": {
                            "understanding_graph_loaded": True,
                            "planning_graph_loaded": True,
                            "llm_loaded": True,
                            "llm_configured": True,
                        },
                    },
                )
            if url == f"{smoke_test.AGENT_URL}/health/dependencies":
                return httpx.Response(
                    200,
                    json={"dependencies": [{"name": "runner", "status": "ok"}]},
                )
            if url == f"{smoke_test.API_URL}/health/dependencies":
                return httpx.Response(
                    200,
                    json={
                        "dependencies": [
                            {"name": "agent", "status": "ok"},
                            {"name": "runner", "status": "ok"},
                        ]
                    },
                )
            raise AssertionError(f"unexpected health URL: {url}")

        def post(self, url, json):
            assert url == f"{smoke_test.AGENT_URL}/v1/plan"
            return httpx.Response(
                200,
                json={
                    "plan": {
                        "model_config": {"architecture": "lenet-5"},
                        "training_config": {
                            "batch_size": {"source": "mvp_scaling"},
                            "learning_rate": {"source": "paper_explicit"},
                        },
                        "assumptions": [
                            {
                                "field_path": "training_config.optimizer",
                                "requires_confirmation": True,
                                "human_confirmed": False,
                            },
                            {
                                "field_path": "training_config.loss_function",
                                "requires_confirmation": True,
                                "human_confirmed": False,
                            },
                        ],
                    }
                },
            )

    monkeypatch.setattr(smoke_test.httpx, "Client", FakeClient)
    smoke_test.failures.clear()
    assert smoke_test.main() == 0
    assert smoke_test.failures == []
