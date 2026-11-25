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
from reproagent_runner.registry import RunRecord, RunRegistry
from reproagent_runner.sandbox import Sandbox, _host_config
from reproagent_schemas import (
    CodeFile,
    ErrorCategory,
    ResourceBudget,
    RunRequest,
    RunResult,
    RunStatus,
    request_fingerprint,
)
from requests.exceptions import ConnectionError as RequestsConnectionError

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


def make_runner_result(run_id, status):
    return RunResult(run_id=run_id, status=status)


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
        if self._behavior.get("chunked_timeout_once") and not self._behavior.get("_timed_out"):
            self._behavior["_timed_out"] = True
            raise RequestsConnectionError(
                "UnixHTTPConnectionPool(host='localhost', port=None): Read timed out."
            )
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


def labels_match(labels, filters):
    for key, values in (filters or {}).items():
        expected = values if isinstance(values, list) else [values]
        for value in expected:
            if key == "label":
                name, _, wanted = value.partition("=")
                if labels.get(name) != wanted:
                    return False
            elif labels.get(key) not in expected:
                return False
    return True


class FakeContainers:
    def __init__(self, behavior):
        self._behavior = behavior
        self.last = None
        self.created = []
        self.created_count = 0

    def create(self, image, command=None, detach=False, **host_config):
        self.created_count += 1
        self.last = FakeContainer(host_config, self._behavior)
        self.last.labels = host_config.get("labels", {})
        self.created.append(self.last)
        return self.last

    def get(self, container_id):
        if container_id not in {container.id for container in self.created}:
            raise RuntimeError("container not found")
        return self.last


class FakeDockerClient:
    def __init__(self, **behavior):
        self.behavior = {"write_metrics": True, **behavior}
        self.containers = FakeContainers(self.behavior)
        self.images = FakeImages()


@pytest.fixture
def settings(tmp_path):
    return RunnerSettings(work_dir=tmp_path, _env_file=None)


@pytest.mark.parametrize("label_kind", ["missing", "other_owner", "other_run"])
def test_journal_cleanup_refuses_unrelated_container_labels(settings, label_kind):
    docker = FakeDockerClient()
    registry = RunRegistry(settings, client=docker)
    request = make_request()
    labels = {}
    if label_kind != "missing":
        labels = {
            "reproagent.managed": "true",
            "reproagent.owner": (
                "another-runner" if label_kind == "other_owner" else registry._owner_id
            ),
            "reproagent.request_id": (
                str(uuid4()) if label_kind == "other_run" else str(request.request_id)
            ),
        }
    container = docker.containers.create("image", labels=labels)

    registry._remove_container(container.id, request.request_id)

    assert not container.killed
    assert not container.removed


def test_image_status_reports_digest_and_dataset(settings):
    with TestClient(create_app(settings, client=FakeDockerClient())) as client:
        body = client.get("/images/status").json()
    assert body["available"] is True
    assert body["digest"] == "sha256:execdigest"
    assert body["dataset_fingerprint"] == "datasetfp"


def test_image_status_reports_missing_image_instead_of_raising(settings):
    class MissingImages:
        def get(self, image):
            raise RuntimeError("No such image: reproagent-exec:latest")

    class MissingClient(FakeDockerClient):
        def __init__(self):
            super().__init__()
            self.images = MissingImages()

    with TestClient(create_app(settings, client=MissingClient())) as client:
        body = client.get("/images/status").json()
    assert body["available"] is False
    assert "No such image" in body["error"]


def test_health(settings):
    with TestClient(create_app(settings, client=FakeDockerClient())) as client:
        body = client.get("/health").json()
    assert body["service"] == "runner"
    assert body["status"] == "ok"
    assert "torch_available" in body["details"]
    assert body["details"]["queued"] == 0
    assert body["details"]["running"] == 0
    assert body["details"]["concurrency_limit"] >= 1


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
        assert config["log_config"].config == {"max-size": "1m", "max-file": "1"}
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

    def test_journal_callback_failure_removes_created_container(self, settings, tmp_path):
        client = FakeDockerClient()
        run_dir = tmp_path / str(uuid4())

        def fail_journal(container_id):
            raise OSError("journal unavailable")

        with pytest.raises(OSError, match="journal unavailable"):
            Sandbox(client, settings).execute(
                make_request(), run_dir, asyncio.Event(), on_container=fail_journal
            )

        assert client.containers.last.started is False
        assert client.containers.last.removed is True

    def test_timeout_kills_container(self, settings, tmp_path):
        client = FakeDockerClient(raise_on_wait=True, write_metrics=False)
        run_dir = tmp_path / str(uuid4())
        result = Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.TIMEOUT
        assert client.containers.last.killed is True
        assert client.containers.last.removed is True

    def test_chunked_wait_timeout_is_retried_not_killed(self, settings, tmp_path):
        # daemon streams /wait chunked: read timeout arrives as ConnectionError
        client = FakeDockerClient(chunked_timeout_once=True, write_metrics=True)
        run_dir = tmp_path / str(uuid4())
        result = Sandbox(client, settings).execute(make_request(), run_dir, asyncio.Event())
        assert result.status is RunStatus.SUCCEEDED
        assert client.containers.last.killed is False

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

    async def test_duplicate_request_is_deduplicated_before_concurrency_limit(self, settings):
        settings.max_concurrent_runs = 1
        gate = threading.Event()
        docker = FakeDockerClient(wait_gate=gate)
        request = make_request()
        async with await self._client(settings, docker) as ac:
            first = await ac.post("/runs", json=request.model_dump(mode="json"))
            assert first.status_code == 202
            run_id = first.json()["run_id"]
            try:
                for _ in range(200):
                    if (await ac.get(f"/runs/{run_id}")).json()["status"] == "running":
                        break
                    await asyncio.sleep(0.01)
                duplicate = await ac.post("/runs", json=request.model_dump(mode="json"))
                assert duplicate.status_code == 202
                assert duplicate.json()["run_id"] == run_id
            finally:
                gate.set()
            for _ in range(200):
                if (await ac.get(f"/runs/{run_id}")).json()["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
        assert docker.containers.created_count == 1

    async def test_conflicting_duplicate_request_returns_409_without_container(self, settings):
        docker = FakeDockerClient()
        request = make_request()
        async with await self._client(settings, docker) as ac:
            first = await ac.post("/runs", json=request.model_dump(mode="json"))
            assert first.status_code == 202
            conflict = request.model_dump(mode="json")
            conflict["seed"] += 1
            response = await ac.post("/runs", json=conflict)
            assert response.status_code == 409
            for _ in range(200):
                status = (await ac.get(f"/runs/{first.json()['run_id']}")).json()["status"]
                if status == "succeeded":
                    break
                await asyncio.sleep(0.01)
        assert docker.containers.created_count == 1

    async def test_restart_fails_active_record_and_duplicate_does_not_retrain(
        self, settings, monkeypatch
    ):
        from contextlib import suppress

        from reproagent_runner import registry as registry_module

        ready = threading.Event()
        release = threading.Event()

        class BlockingSandbox:
            def __init__(self, client, *args, **kwargs):
                self._client = client

            def execute(self, request, run_dir, cancel_event, on_container=None):
                container = self._client.containers.create(
                    "reproagent-exec:latest",
                    labels={
                        "reproagent.managed": "true",
                        "reproagent.owner": first._owner_id,
                        "reproagent.request_id": str(request.request_id),
                    },
                )
                container.id = "owned-container"
                if on_container is not None:
                    on_container(container.id)
                ready.set()
                release.wait(timeout=10)
                return make_runner_result(request.request_id, RunStatus.SUCCEEDED)

        class RecoveryContainers(FakeContainers):
            def create(self, image, command=None, detach=False, **host_config):
                container = super().create(image, command, detach, **host_config)
                container.id = "owned-container"
                return container

            def list(self, all=False, filters=None):
                return [
                    container for container in self.created
                    if labels_match(container.labels, filters)
                ]

        monkeypatch.setattr(registry_module, "Sandbox", BlockingSandbox)
        docker = FakeDockerClient()
        docker.containers = RecoveryContainers(docker.behavior)
        request = make_request()
        first = RunRegistry(settings, client=docker)
        accepted = first.start(request)
        for _ in range(200):
            if ready.is_set():
                break
            await asyncio.sleep(0.01)
        assert ready.is_set()
        first._runs[accepted.run_id].task.cancel()
        with suppress(asyncio.CancelledError):
            await first._runs[accepted.run_id].task
        release.set()

        recovered = RunRegistry(settings, client=docker)
        result = recovered.get(accepted.run_id)
        assert result.status is RunStatus.FAILED
        assert "restart" in result.error.lower()
        assert docker.containers.last.killed is True
        assert docker.containers.last.removed is True
        duplicate = recovered.start(request)
        assert duplicate.run_id == accepted.run_id
        assert duplicate.status is RunStatus.FAILED
        assert docker.containers.created_count == 1

    async def test_restart_preserves_cancellation_won_during_execution(self, settings, monkeypatch):
        from contextlib import suppress

        from reproagent_runner import registry as registry_module

        ready = threading.Event()
        release = threading.Event()

        class BlockingSandbox:
            def __init__(self, client, *args, **kwargs):
                self._client = client

            def execute(self, request, run_dir, cancel_event, on_container=None):
                container = self._client.containers.create(
                    "reproagent-exec:latest",
                    labels={
                        "reproagent.managed": "true",
                        "reproagent.owner": first._owner_id,
                        "reproagent.request_id": str(request.request_id),
                    },
                )
                container.id = "cancel-window-container"
                if on_container is not None:
                    on_container(container.id)
                ready.set()
                release.wait(timeout=10)
                return make_runner_result(request.request_id, RunStatus.SUCCEEDED)

        class RecoveryContainers(FakeContainers):
            def create(self, image, command=None, detach=False, **host_config):
                container = super().create(image, command, detach, **host_config)
                container.id = "cancel-window-container"
                return container

            def list(self, all=False, filters=None):
                return [
                    container
                    for container in self.created
                    if labels_match(container.labels, filters)
                ]

        monkeypatch.setattr(registry_module, "Sandbox", BlockingSandbox)
        docker = FakeDockerClient()
        docker.containers = RecoveryContainers(docker.behavior)
        request = make_request()
        first = RunRegistry(settings, client=docker)
        accepted = first.start(request)
        for _ in range(200):
            if ready.is_set():
                break
            await asyncio.sleep(0.01)
        assert ready.is_set()

        cancel_response = first.cancel(accepted.run_id)
        first._runs[accepted.run_id].task.cancel()
        with suppress(asyncio.CancelledError):
            await first._runs[accepted.run_id].task
        release.set()

        recovered = RunRegistry(settings, client=docker)
        result = recovered.get(accepted.run_id)
        assert cancel_response.status is RunStatus.CANCELLED
        assert result.status is RunStatus.CANCELLED
        assert docker.containers.last.killed is True
        assert docker.containers.last.removed is True
        duplicate = recovered.start(request)
        assert duplicate.run_id == accepted.run_id
        assert duplicate.status is RunStatus.CANCELLED
        assert docker.containers.created_count == 1

    async def test_restart_removes_owned_orphan_but_not_unrelated_container(self, settings):
        class OrphanContainers(FakeContainers):
            def __init__(self, owner):
                super().__init__({})
                self.owned = FakeContainer({}, {})
                self.owned.id = "orphan"
                self.owned.labels = {
                    "reproagent.managed": "true",
                    "reproagent.owner": owner,
                }
                self.unrelated = FakeContainer({}, {})
                self.unrelated.id = "unrelated"
                self.unrelated.labels = {"application": "something-else"}

            def list(self, all=False, filters=None):
                return [
                    container for container in (self.owned, self.unrelated)
                    if labels_match(container.labels, filters)
                ]

            def get(self, container_id):
                if container_id == self.owned.id:
                    return self.owned
                raise RuntimeError("container not found")

        docker = FakeDockerClient()
        owner_file = settings.work_dir / "runner-owner"
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        owner_file.write_text("stable-owner", encoding="utf-8")
        containers = OrphanContainers(owner_file.read_text(encoding="utf-8"))
        docker.containers = containers

        RunRegistry(settings, client=docker)

        assert containers.owned.killed is True
        assert containers.owned.removed is True
        assert containers.unrelated.killed is False
        assert containers.unrelated.removed is False

    async def test_completed_result_survives_restart_and_ttl_reap(self, settings):
        docker = FakeDockerClient()
        request = make_request()
        first = RunRegistry(settings, client=docker)
        accepted = first.start(request)
        for _ in range(200):
            if first.get(accepted.run_id).status is RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        expected = first.get(accepted.run_id)

        recovered = RunRegistry(settings, client=docker)
        assert recovered.get(accepted.run_id) == expected
        assert recovered.start(request).run_id == accepted.run_id
        recovered._runs[accepted.run_id].finished_at = time.time() - settings.run_ttl_seconds - 1
        recovered.reap_expired()
        assert accepted.run_id not in recovered._runs
        assert not (settings.work_dir / str(accepted.run_id)).exists()
        assert not (settings.work_dir / ".journal" / f"{accepted.run_id}.json").exists()

    async def test_corrupt_journal_does_not_block_valid_recovery(self, settings, caplog):
        docker = FakeDockerClient()
        request = make_request()
        first = RunRegistry(settings, client=docker)
        accepted = first.start(request)
        for _ in range(200):
            if first.get(accepted.run_id).status is RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        journal_dir = settings.work_dir / ".journal"
        (journal_dir / "corrupt.json").write_text("{not-json", encoding="utf-8")

        recovered = RunRegistry(settings, client=docker)

        assert recovered.get(accepted.run_id).status is RunStatus.SUCCEEDED
        assert "journal" in caplog.text.lower()

    @pytest.mark.parametrize(
        "corruption",
        [
            "bad_timestamp",
            "terminal_without_result",
            "result_status_mismatch",
            "active_with_result",
        ],
    )
    def test_structurally_corrupt_journal_is_isolated(self, settings, caplog, corruption):
        docker = FakeDockerClient()
        registry = RunRegistry(settings, client=docker)
        valid_request = make_request()
        valid_result = make_runner_result(valid_request.request_id, RunStatus.SUCCEEDED)
        valid = RunRecord(
            request=valid_request,
            run_id=valid_request.request_id,
            run_dir=settings.work_dir / str(valid_request.request_id),
            status=RunStatus.SUCCEEDED,
            result=valid_result,
            finished_at=time.time(),
            fingerprint=request_fingerprint(valid_request),
        )
        registry._write_record(valid)
        corrupt_request = make_request()
        corrupt = RunRecord(
            request=corrupt_request,
            run_id=corrupt_request.request_id,
            run_dir=settings.work_dir / str(corrupt_request.request_id),
            status=RunStatus.RUNNING,
            fingerprint=request_fingerprint(corrupt_request),
        )
        registry._write_record(corrupt)
        path = registry._journal_path(corrupt_request.request_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if corruption == "bad_timestamp":
            payload.update(
                status="succeeded",
                result=make_runner_result(
                    corrupt_request.request_id, RunStatus.SUCCEEDED
                ).model_dump(mode="json"),
                finished_at="bad",
            )
        elif corruption == "terminal_without_result":
            payload.update(status="succeeded", result=None, finished_at=time.time())
        elif corruption == "result_status_mismatch":
            payload.update(
                status="succeeded",
                result=make_runner_result(
                    corrupt_request.request_id, RunStatus.FAILED
                ).model_dump(mode="json"),
                finished_at=time.time(),
            )
        else:
            payload.update(
                status="running",
                result=make_runner_result(
                    corrupt_request.request_id, RunStatus.SUCCEEDED
                ).model_dump(mode="json"),
                finished_at=time.time(),
            )
        path.write_text(json.dumps(payload), encoding="utf-8")

        recovered = RunRegistry(settings, client=docker)

        assert recovered.get(valid_request.request_id) == valid_result
        assert corrupt_request.request_id not in recovered._runs
        assert "journal" in caplog.text.lower()

    async def test_reap_expired_evicts_and_cleans(self, settings):
        registry = RunRegistry(settings, client=FakeDockerClient())
        accepted = registry.start(make_request())
        for _ in range(200):
            if registry.get(accepted.run_id).status is RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        record = registry._runs[accepted.run_id]
        assert record.run_dir.exists()
        record.finished_at = time.time() - settings.run_ttl_seconds - 1
        registry.reap_expired()
        assert accepted.run_id not in registry._runs
        assert not record.run_dir.exists()