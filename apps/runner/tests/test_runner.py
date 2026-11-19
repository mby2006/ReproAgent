import asyncio
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport
from reproagent_runner.config import RunnerSettings
from reproagent_runner.main import create_app
from reproagent_runner.registry import RunRegistry
from reproagent_runner.sandbox import Sandbox, _host_config
from reproagent_schemas import CodeFile, ErrorCategory, ResourceBudget, RunRequest, RunStatus

METRICS = {
    "schema_version": "1.0",
    "target_metric": "accuracy",
    "metric_value": 0.97,
    "accuracy": 0.97,
    "train_loss": 0.1,
    "test_loss": 0.12,
    "epochs_completed": 3,
    "train_samples": 10000,
    "test_samples": 10000,
    "seed": 42,
    "runtime_seconds": 1.0,
}


def make_request(**overrides) -> RunRequest:
    base = dict(
        experiment_id=uuid4(),
        experiment_plan_id=uuid4(),
        code_files=[CodeFile(path="train.py", content="print('hi')")],
        dependencies=["torch==2.4.1"],
    )
    base.update(overrides)
    return RunRequest(**base)


class FakeImage:
    id = "sha256:execdigest"
    labels = {
        "reproagent.python_version": "3.12.0",
        "reproagent.dataset_fingerprint": "datasetfp",
    }


class FakeImages:
    def get(self, image):
        return FakeImage()


class FakeContainer:
    def __init__(self, host_config, behavior):
        self.id = "container123"
        self._binds = host_config.get("binds", [])
        self._volumes = host_config.get("volumes", {})
        self._behavior = behavior
        self.killed = False
        self.removed = False
        self.started = False

    def _output_dir(self):
        for bind in self._binds:
            if bind.endswith(":/output:rw"):
                return Path(bind.split(":/output:rw")[0])
        for source, spec in self._volumes.items():
            if spec.get("bind") == "/output":
                return Path(source)
        return None

    def start(self):
        self.started = True
        output = self._output_dir()
        if self._behavior.get("write_metrics") and output is not None:
            (output / "metrics.json").write_text(json.dumps(METRICS), encoding="utf-8")
        if self._behavior.get("big_output_bytes") and output is not None:
            (output / "big.bin").write_bytes(b"x" * self._behavior["big_output_bytes"])

    def wait(self, timeout=None):
        if self._behavior.get("raise_on_wait"):
            raise RuntimeError("simulated hard timeout")
        gate = self._behavior.get("wait_gate")
        if gate is not None:
            gate.wait(timeout=10)
        return {"StatusCode": self._behavior.get("exit_code", 0)}

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True

    def logs(self, stdout=True, stderr=False, timestamps=False):
        if stdout:
            return self._behavior.get("stdout", b"ok\n")
        return self._behavior.get("stderr", b"")


class FakeContainers:
    def __init__(self, behavior):
        self._behavior = behavior
        self.last = None

    def create(self, image, command=None, detach=False, **host_config):
        self.last = FakeContainer(host_config, self._behavior)
        return self.last

    def get(self, container_id):
        return self.last


class FakeDockerClient:
    def __init__(self, **behavior):
        self.behavior = {"write_metrics": True, **behavior}
        self.containers = FakeContainers(self.behavior)
        self.images = FakeImages()


@pytest.fixture
def settings(tmp_path):
    return RunnerSettings(work_dir=tmp_path, _env_file=None)


def test_health(settings):
    with TestClient(create_app(settings, client=FakeDockerClient())) as client:
        body = client.get("/health").json()
    assert body["service"] == "runner"
    assert body["status"] == "ok"
    assert "torch_available" in body["details"]


def test_settings_have_no_secret_fields():
    for name in RunnerSettings.model_fields:
        assert not any(word in name for word in ("key", "token", "secret", "password"))


def test_settings_read_runner_prefixed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("RUNNER_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("ENVIRONMENT", "testing")
    settings = RunnerSettings(_env_file=None)
    assert settings.work_dir == tmp_path
    assert settings.timeout_seconds == 120
    assert settings.environment == "testing"


class TestSandboxLockdown:
    def test_host_config_is_locked_down(self, settings, tmp_path):
        request = make_request()
        config = _host_config(request, settings, tmp_path / "code", tmp_path / "output")
        assert config["network_mode"] == "none"
        assert config["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in config["security_opt"]
        assert config["read_only"] is True
        assert config["user"] == "1000:1000"
        assert config["pids_limit"] == request.resource_budget.pids_limit
        assert config["mem_limit"] == f"{request.resource_budget.memory_mb}m"
        assert config["nano_cpus"] == int(request.resource_budget.cpu_count * 1e9)
        assert any(b.endswith(":/work/code:ro") for b in config["binds"])
        assert any(b.endswith(":/output:rw") for b in config["binds"])

    def test_successful_run_collects_metrics_and_audit(self, settings, tmp_path):
        run_dir = tmp_path / str(uuid4())
        sandbox = Sandbox(FakeDockerClient(), settings)
        result = sandbox.execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.SUCCEEDED
        assert result.exit_code == 0
        assert result.metrics["accuracy"] == 0.97
        assert "metrics.json" in result.artifacts
        assert result.audit.image_digest == "sha256:execdigest"
        assert result.audit.python_version == "3.12.0"
        assert result.audit.dataset_fingerprint == "datasetfp"
        assert result.audit.code_hash

    def test_container_destroyed_after_run(self, settings, tmp_path):
        client = FakeDockerClient()
        run_dir = tmp_path / str(uuid4())
        Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert client.containers.last.removed is True

    def test_timeout_kills_container(self, settings, tmp_path):
        client = FakeDockerClient(raise_on_wait=True, write_metrics=False)
        run_dir = tmp_path / str(uuid4())
        result = Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.TIMEOUT
        assert client.containers.last.killed is True
        assert client.containers.last.removed is True

    def test_nonzero_exit_is_failed(self, settings, tmp_path):
        client = FakeDockerClient(exit_code=1, write_metrics=False, stderr=b"Traceback: boom")
        run_dir = tmp_path / str(uuid4())
        result = Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.FAILED
        assert "boom" in result.stderr

    def test_missing_metrics_is_metric_error(self, settings, tmp_path):
        client = FakeDockerClient(write_metrics=False)
        run_dir = tmp_path / str(uuid4())
        result = Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.FAILED
        assert result.error_category.value == "METRIC_ERROR"

    def test_disk_quota_exceeded_kills_container(self, settings, tmp_path):
        # /output is a bind mount storage_opt cannot quota; the poll loop must
        # enforce disk_mb by killing the container when the directory grows past it
        client = FakeDockerClient(write_metrics=False, big_output_bytes=2 * 1024 * 1024)
        run_dir = tmp_path / str(uuid4())
        request = make_request(resource_budget=ResourceBudget(disk_mb=1))
        result = Sandbox(client, settings).execute(request, run_dir, asyncio.Event())
        assert result.status is RunStatus.FAILED
        assert result.error_category is ErrorCategory.UNKNOWN_ERROR
        assert "disk quota" in result.error
        assert client.containers.last.killed is True
        assert client.containers.last.removed is True

    def test_metrics_filename_traversal_rejected(self):
        with pytest.raises(ValueError, match="unsafe metrics_filename"):
            make_request(metrics_filename="../../etc/passwd")


class TestRunsAPI:
    async def _client(self, settings, docker):
        app = create_app(settings, client=docker)
        app.state.registry = RunRegistry(settings, client=docker)
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://runner.test")

    async def test_run_lifecycle(self, settings):
        docker = FakeDockerClient()
        async with await self._client(settings, docker) as ac:
            accepted = await ac.post("/runs", json=make_request().model_dump(mode="json"))
            assert accepted.status_code == 202
            run_id = accepted.json()["run_id"]
            assert accepted.json()["status"] == "queued"
            for _ in range(200):
                result = await ac.get(f"/runs/{run_id}")
                if result.json()["status"] in {"succeeded", "failed", "timeout", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert result.json()["status"] == "succeeded"
            assert result.json()["metrics"]["accuracy"] == 0.97
            artifacts = await ac.get(f"/runs/{run_id}/artifacts")
            assert "metrics.json" in artifacts.json()
            logs = await ac.get(f"/runs/{run_id}/logs")
            assert logs.status_code == 200
            # terminal logs come from RunResult, which strips edge whitespace
            assert logs.json()["stdout"] == "ok"

    async def test_logs_live_while_running(self, settings):
        gate = threading.Event()
        docker = FakeDockerClient(stdout=b"epoch 1 live\n", wait_gate=gate)
        async with await self._client(settings, docker) as ac:
            accepted = await ac.post("/runs", json=make_request().model_dump(mode="json"))
            run_id = accepted.json()["run_id"]
            try:
                live = None
                for _ in range(500):
                    live = (await ac.get(f"/runs/{run_id}/logs")).json()
                    if live["stdout"]:
                        break
                    await asyncio.sleep(0.01)
                assert live is not None and live["stdout"] == "epoch 1 live\n"
                status = (await ac.get(f"/runs/{run_id}")).json()["status"]
                assert status == "running"
            finally:
                gate.set()
            for _ in range(500):
                result = await ac.get(f"/runs/{run_id}")
                if result.json()["status"] in {"succeeded", "failed", "timeout", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert result.json()["status"] == "succeeded"
            logs = (await ac.get(f"/runs/{run_id}/logs")).json()
            assert logs["stdout"] == "epoch 1 live"

    async def test_unknown_run_is_404(self, settings):
        async with await self._client(settings, FakeDockerClient()) as ac:
            assert (await ac.get(f"/runs/{uuid4()}")).status_code == 404
            assert (await ac.post(f"/runs/{uuid4()}/cancel")).status_code == 404
            assert (await ac.get(f"/runs/{uuid4()}/artifacts")).status_code == 404
            assert (await ac.get(f"/runs/{uuid4()}/logs")).status_code == 404

    async def test_cancel_run(self, settings):
        docker = FakeDockerClient(raise_on_wait=True, write_metrics=False)
        async with await self._client(settings, docker) as ac:
            accepted = await ac.post("/runs", json=make_request().model_dump(mode="json"))
            run_id = accepted.json()["run_id"]
            await asyncio.sleep(0.05)
            cancelled = await ac.post(f"/runs/{run_id}/cancel")
            assert cancelled.status_code == 200
            for _ in range(200):
                result = await ac.get(f"/runs/{run_id}")
                if result.json()["status"] in {"succeeded", "failed", "timeout", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert result.json()["status"] in {"cancelled", "timeout"}

    async def test_invalid_request_rejected(self, settings):
        async with await self._client(settings, FakeDockerClient()) as ac:
            bad = make_request().model_dump(mode="json")
            bad["dependencies"] = ["requests==2.0.0"]
            assert (await ac.post("/runs", json=bad)).status_code == 422

    async def test_concurrency_limit_returns_429(self, settings):
        settings.max_concurrent_runs = 1
        gate = threading.Event()
        docker = FakeDockerClient(wait_gate=gate)
        async with await self._client(settings, docker) as ac:
            first = await ac.post("/runs", json=make_request().model_dump(mode="json"))
            assert first.status_code == 202
            run_id = first.json()["run_id"]
            try:
                for _ in range(200):
                    if (await ac.get(f"/runs/{run_id}")).json()["status"] == "running":
                        break
                    await asyncio.sleep(0.01)
                second = await ac.post("/runs", json=make_request().model_dump(mode="json"))
                assert second.status_code == 429
            finally:
                gate.set()

    async def test_reap_expired_evicts_and_cleans(self, settings):
        registry = RunRegistry(settings, client=FakeDockerClient())
        accepted = registry.start(make_request())
        for _ in range(200):
            if registry.get(accepted.run_id).status is RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        record = registry._runs[accepted.run_id]
        assert record.run_dir.exists()
        record.finished_at = time.monotonic() - settings.run_ttl_seconds - 1
        registry.reap_expired()
        assert accepted.run_id not in registry._runs
        assert not record.run_dir.exists()