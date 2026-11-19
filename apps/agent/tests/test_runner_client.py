from uuid import uuid4

import httpx
import pytest
from reproagent_agent.clients.runner import RunnerClient
from reproagent_agent.config import AgentSettings
from reproagent_schemas import CodeFile, RunRequest, RunStatus


def make_request() -> RunRequest:
    return RunRequest(
        experiment_id=uuid4(),
        experiment_plan_id=uuid4(),
        code_files=[CodeFile(path="train.py", content="print('hello')")],
    )


def make_client(handler) -> RunnerClient:
    settings = AgentSettings(runner_base_url="http://runner.test", _env_file=None)
    return RunnerClient(settings, transport=httpx.MockTransport(handler))


async def test_start_run_returns_accepted():
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/runs"
        assert request.method == "POST"
        return httpx.Response(202, json={"run_id": str(run_id), "status": "queued"})

    client = make_client(handler)
    accepted = await client.start_run(make_request())
    assert accepted.run_id == run_id
    assert accepted.status is RunStatus.QUEUED
    await client.aclose()


async def test_get_run_parses_result():
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/runs/{run_id}"
        return httpx.Response(
            200,
            json={
                "run_id": str(run_id),
                "status": "succeeded",
                "exit_code": 0,
                "execution_time": 12.5,
                "metrics": {"accuracy": 0.97},
            },
        )

    client = make_client(handler)
    result = await client.get_run(run_id)
    assert result.status is RunStatus.SUCCEEDED
    assert result.metrics["accuracy"] == 0.97
    await client.aclose()


async def test_cancel_run_posts_to_cancel():
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/runs/{run_id}/cancel"
        assert request.method == "POST"
        return httpx.Response(
            200, json={"run_id": str(run_id), "status": "cancelled", "execution_time": 0}
        )

    client = make_client(handler)
    result = await client.cancel_run(run_id)
    assert result.status is RunStatus.CANCELLED
    await client.aclose()


async def test_get_artifacts_returns_list():
    run_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/runs/{run_id}/artifacts"
        return httpx.Response(200, json=["metrics.json", "model.pt"])

    client = make_client(handler)
    artifacts = await client.get_artifacts(run_id)
    assert artifacts == ["metrics.json", "model.pt"]
    await client.aclose()


async def test_start_run_propagates_http_error():
    client = make_client(lambda request: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await client.start_run(make_request())
    await client.aclose()