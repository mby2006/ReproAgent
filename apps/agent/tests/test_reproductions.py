from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.main import create_app


def test_placeholder_reproduction_and_job_routes_are_not_registered():
    app = create_app(AgentSettings(_env_file=None))
    paths = set(app.openapi()["paths"])
    assert not any(path.startswith("/v1/reproductions") for path in paths)
    assert not any(path.startswith("/v1/jobs") for path in paths)


def test_placeholder_reproduction_and_job_requests_return_404():
    with TestClient(create_app(AgentSettings(_env_file=None))) as client:
        assert client.post("/v1/reproductions", json={}).status_code == 404
        assert client.get("/v1/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
