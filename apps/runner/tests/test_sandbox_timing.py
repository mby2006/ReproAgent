"""Segment timing contracts for the Sandbox and durable Runner Registry."""

import json
import threading
from uuid import uuid4

import pytest
from docker.errors import APIError
from reproagent_runner import registry as registry_module
from reproagent_runner.config import RunnerSettings
from reproagent_runner.registry import RunRecord, RunRegistry
from reproagent_runner.sandbox import Sandbox
from reproagent_schemas import CodeFile, RunRequest, RunResult, RunStatus, request_fingerprint


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeImage:
    id = "sha256:exec"
    labels = {"reproagent.dataset_fingerprint": "dataset-v1"}


class FakeImages:
    def get(self, image):
        return FakeImage()


class FakeContainer:
    id = "container-1"

    def __init__(self, clock, *, start_error=None):
        self.clock = clock
        self.start_error = start_error
        self.removed = False

    def start(self):
        self.clock.advance(3.0)
        if self.start_error is not None:
            raise self.start_error

    def wait(self, timeout=None):
        self.clock.advance(4.0)
        return {"StatusCode": 0}

    def logs(self, stdout=True, stderr=False, timestamps=False):
        self.clock.advance(1.0)
        return b"ok\n" if stdout else b""

    def remove(self, force=False):
        self.clock.advance(2.0)
        self.removed = True

    def kill(self):
        pass


class FakeContainers:
    def __init__(self, clock, *, create_error=None, start_error=None):
        self.clock = clock
        self.create_error = create_error
        self.start_error = start_error
        self.created = []

    def create(self, image, command=None, detach=False, **kwargs):
        self.clock.advance(2.0)
        if self.create_error is not None:
            raise self.create_error
        container = FakeContainer(self.clock, start_error=self.start_error)
        self.created.append(container)
        return container

    def list(self, all=False, filters=None):
        return []

    def get(self, container_id):
        raise RuntimeError("container not found")


class FakeDockerClient:
    def __init__(self, clock, *, create_error=None, start_error=None):
        self.containers = FakeContainers(
            clock, create_error=create_error, start_error=start_error
        )
        self.images = FakeImages()


def make_request():
    return RunRequest(
        experiment_id=uuid4(),
        experiment_plan_id=uuid4(),
        code_files=[CodeFile(path="train.py", content="print('ok')")],
    )


def test_sandbox_records_setup_training_and_cleanup_with_one_clock(monkeypatch, tmp_path):
    from reproagent_schemas import RunTimings

    clock = Clock()
    monkeypatch.setattr("reproagent_runner.sandbox.time.monotonic", clock)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    run_dir = tmp_path / str(uuid4())
    (run_dir / "output").mkdir(parents=True)
    (run_dir / "output" / "metrics.json").write_text(json.dumps({
        "target_metric": "accuracy", "metric_value": 1.0, "epochs_completed": 1,
        "train_samples": 1, "test_samples": 1, "seed": 42, "runtime_seconds": 4.0,
    }), encoding="utf-8")
    result = Sandbox(FakeDockerClient(clock), settings).execute(
        make_request(), run_dir, threading.Event()
    )

    assert result.status is RunStatus.SUCCEEDED
    assert isinstance(result.timings, RunTimings)
    assert result.timings.queue_seconds is None
    assert result.timings.runner_queue_seconds is None
    assert result.timings.container_setup_seconds == 5.0
    assert result.timings.training_evaluation_seconds == 4.0
    assert result.timings.container_cleanup_seconds == 4.0
    assert result.timings.container_lifecycle_seconds == 13.0
    assert result.timings.container_lifecycle_seconds == (
        result.timings.container_setup_seconds
        + result.timings.training_evaluation_seconds
        + result.timings.container_cleanup_seconds
    )
    assert result.timings.runner_total_seconds is None
    assert result.timings.total_seconds is None


def test_failed_container_create_keeps_real_setup_and_unexecuted_stages_none(
    monkeypatch, tmp_path
):
    clock = Clock()
    monkeypatch.setattr("reproagent_runner.sandbox.time.monotonic", clock)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    client = FakeDockerClient(clock, create_error=APIError("create failed"))

    result = Sandbox(client, settings).execute(
        make_request(), tmp_path / str(uuid4()), threading.Event()
    )

    assert result.status is RunStatus.FAILED
    assert result.timings.container_setup_seconds == 2.0
    assert result.timings.training_evaluation_seconds is None
    assert result.timings.container_cleanup_seconds is None
    assert result.timings.container_lifecycle_seconds is None
    assert result.timings.report_generation_seconds is None
    assert result.timings.runner_queue_seconds is None


def test_failed_container_start_keeps_attempted_setup_and_cleanup_without_training(
    monkeypatch, tmp_path
):
    clock = Clock()
    monkeypatch.setattr("reproagent_runner.sandbox.time.monotonic", clock)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    client = FakeDockerClient(clock, start_error=APIError("start failed"))

    result = Sandbox(client, settings).execute(
        make_request(), tmp_path / str(uuid4()), threading.Event()
    )

    assert result.status is RunStatus.FAILED
    assert "could not be started" in result.error
    assert result.timings.container_setup_seconds == 5.0
    assert result.timings.training_evaluation_seconds is None
    assert result.timings.container_cleanup_seconds == 4.0
    assert result.timings.container_lifecycle_seconds is None
    assert client.containers.created[0].removed is True


def test_cancel_before_create_does_not_fake_zero_duration(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr("reproagent_runner.sandbox.time.monotonic", clock)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    cancel = threading.Event()
    cancel.set()

    result = Sandbox(FakeDockerClient(clock), settings).execute(
        make_request(), tmp_path / str(uuid4()), cancel
    )

    assert result.status is RunStatus.CANCELLED
    assert result.timings.container_setup_seconds is None
    assert result.timings.training_evaluation_seconds is None
    assert result.timings.container_cleanup_seconds is None
    assert result.timings.container_lifecycle_seconds is None


@pytest.mark.asyncio
async def test_registry_journals_acceptance_and_records_queue_and_total(
    monkeypatch, tmp_path
):
    wall_clock = Clock(100.0)
    runner_clock = Clock(10.0)
    monkeypatch.setattr(registry_module.time, "time", wall_clock)
    monkeypatch.setattr(registry_module.time, "perf_counter", runner_clock)

    class FakeSandbox:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, request, run_dir, cancel_event, on_container=None):
            runner_clock.advance(2.5)
            return RunResult(
                run_id=request.request_id,
                status=RunStatus.SUCCEEDED,
                timings={"container_setup_seconds": 1.0},
            )

    monkeypatch.setattr(registry_module, "Sandbox", FakeSandbox)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    client = FakeDockerClient(Clock())
    registry = RunRegistry(settings, client=client)
    request = make_request()
    accepted = registry.start(request)
    runner_clock.advance(0.5)
    wall_clock.advance(50.0)

    await registry._runs[accepted.run_id].task
    result = registry.get(accepted.run_id)
    journal = json.loads(
        (settings.work_dir / ".journal" / f"{accepted.run_id}.json").read_text(encoding="utf-8")
    )

    assert journal["accepted_at"] == 100.0
    assert "accepted_monotonic_at" not in journal
    assert registry._runs[accepted.run_id].accepted_monotonic_at == 10.0
    assert result.timings.runner_queue_seconds == 0.5
    assert result.timings.runner_total_seconds == 2.5
    assert result.timings.container_setup_seconds == 1.0
    assert result.timings.queue_seconds is None

    recovered = RunRegistry(settings, client=client)
    assert recovered._runs[accepted.run_id].accepted_at == 100.0
    assert recovered._runs[accepted.run_id].accepted_monotonic_at is None
    assert recovered.get(accepted.run_id) == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted_monotonic_at",
    [11.0, float("nan"), float("inf"), float("-inf")],
    ids=["backward", "nan", "positive-infinity", "negative-infinity"],
)
async def test_registry_invalid_monotonic_queue_elapsed_stays_none(
    monkeypatch, tmp_path, accepted_monotonic_at
):
    wall_clock = Clock(100.0)
    runner_clock = Clock(accepted_monotonic_at)
    monkeypatch.setattr(registry_module.time, "time", wall_clock)
    monkeypatch.setattr(registry_module.time, "perf_counter", runner_clock)

    class FakeSandbox:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, request, run_dir, cancel_event, on_container=None):
            runner_clock.value = 10.0
            runner_clock.advance(2.5)
            return RunResult(run_id=request.request_id, status=RunStatus.SUCCEEDED)

    monkeypatch.setattr(registry_module, "Sandbox", FakeSandbox)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    registry = RunRegistry(settings, client=FakeDockerClient(Clock()))
    accepted = registry.start(make_request())
    runner_clock.value = 10.0
    wall_clock.advance(1.0)

    await registry._runs[accepted.run_id].task

    assert registry.get(accepted.run_id).timings.runner_queue_seconds is None
    assert registry.get(accepted.run_id).timings.runner_total_seconds == 2.5


def test_registry_recovery_keeps_absent_timing_stages_none(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_module.time, "time", lambda: 100.0)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    registry = RunRegistry(settings, client=FakeDockerClient(Clock()))
    request = make_request()
    record = RunRecord(
        request=request,
        run_id=request.request_id,
        run_dir=settings.work_dir / str(request.request_id),
        status=RunStatus.SUCCEEDED,
        result=RunResult(run_id=request.request_id, status=RunStatus.SUCCEEDED),
        finished_at=100.0,
        fingerprint=request_fingerprint(request),
        accepted_at=None,
    )
    registry._write_record(record)

    recovered = RunRegistry(settings, client=FakeDockerClient(Clock()))
    result = recovered.get(request.request_id)

    assert recovered._runs[request.request_id].accepted_at is None
    assert recovered._runs[request.request_id].accepted_monotonic_at is None
    assert result.timings.runner_queue_seconds is None
    assert result.timings.runner_total_seconds is None
    assert result.timings.container_lifecycle_seconds is None


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING])
def test_registry_recovered_inflight_never_fabricates_queue_timing(monkeypatch, tmp_path, status):
    monkeypatch.setattr(registry_module.time, "time", lambda: 100.0)
    settings = RunnerSettings(work_dir=tmp_path, _env_file=None)
    registry = RunRegistry(settings, client=FakeDockerClient(Clock()))
    request = make_request()
    record = RunRecord(
        request=request,
        run_id=request.request_id,
        run_dir=settings.work_dir / str(request.request_id),
        status=status,
        finished_at=None,
        fingerprint=request_fingerprint(request),
        accepted_at=100.0,
    )
    registry._write_record(record)

    recovered = RunRegistry(settings, client=FakeDockerClient(Clock()))
    result = recovered.get(request.request_id)

    assert recovered._runs[request.request_id].accepted_monotonic_at is None
    assert result.status is RunStatus.FAILED
    assert result.timings.runner_queue_seconds is None
    assert result.timings.runner_total_seconds is None
