from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(AgentSettings(_env_file=None))) as test_client:
        yield test_client


def test_start_reproduction_validates_then_stub(client):
    response = client.post(
        "/v1/reproductions",
        json={"user_id": str(uuid4()), "paper_id": str(uuid4()), "title": "LeNet-5"},
    )
    assert response.status_code == 501


def test_start_reproduction_rejects_missing_ownership(client):
    response = client.post("/v1/reproductions", json={"paper_id": str(uuid4()), "title": "LeNet-5"})
    assert response.status_code == 422


def test_get_job_stub(client):
    response = client.get(f"/v1/jobs/{uuid4()}")
    assert response.status_code == 501
