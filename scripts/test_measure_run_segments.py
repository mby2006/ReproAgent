"""Offline fake-HTTP contracts for the three-run segment measurement script."""

import copy
import json
import math
from pathlib import Path
from uuid import UUID

import httpx
import pytest


def load_measurement():
    try:
        from scripts import measure_run_segments
    except ImportError as exc:  # RED before the script exists, without collection errors
        pytest.fail(f"measurement script is unavailable: {exc}")
    return measure_run_segments


def environment():
    return {
        "image_digest": "sha256:image",
        "dataset_fingerprint": "dataset-v1",
        "dependency_lock_hash": "deps-v1",
        "plan_hash": "plan-v1",
        "code_hash": "code-v1",
        "seed": 42,
        "resource_budget": {
            "max_seconds": 600,
            "memory_mb": 512,
            "cpu_count": 1.0,
            "pids_limit": 256,
            "disk_mb": 256,
            "device": "cpu",
        },
    }


@pytest.fixture
def sample_runs():
    return [
        {
            "run_id": str(UUID(int=index + 1)),
            "experiment_id": str(UUID(int=10)),
            "plan_id": str(UUID(int=20)),
            "plan_version": 1,
            "code_version": 1,
            "attempt": 2,
            "status": "succeeded",
            "queue_seconds": float(index + 1),
            "container_lifecycle_seconds": 6.0,
            "training_evaluation_seconds": 2.0,
            "report_generation_seconds": 0.5,
            "total_seconds": float(index + 7.5),
            "environment": environment(),
            "report": {
                "version": 1,
                "execution_attempt": 2,
                "report_json": {
                    "references": {
                        "experiment_id": str(UUID(int=10)),
                        "plan_id": str(UUID(int=20)),
                        "plan_version": 1,
                        "code_version": 1,
                        "code_hash": "code-v1",
                        "execution_attempt": 2,
                    }
                },
            },
        }
        for index in range(3)
    ]


class FakeMeasurementServer:
    def __init__(self, *, missing_report=False, never_terminal=False):
        self.missing_report = missing_report
        self.never_terminal = never_terminal
        self.requests = []
        self.keys = []
        self.run_ids = []
        self.polls = {}
        self.active_run = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/v1/experiments/experiment-1":
            return httpx.Response(200, json={
                "id": "experiment-1",
                "plan": {
                    "id": str(UUID(int=20)),
                    "version": 1,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                    "plan": {"runtime_budget": {"seed": 42, "max_seconds": 600}},
                },
            })
        if request.method == "POST" and path == "/v1/experiments/experiment-1/runs":
            assert self.active_run is None, "runs were not measured sequentially"
            key = request.headers["Idempotency-Key"]
            self.keys.append(key)
            run_id = str(UUID(int=100 + len(self.run_ids) + 1))
            self.run_ids.append(run_id)
            self.active_run = run_id
            self.polls[run_id] = 0
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        if request.method == "GET" and path.startswith("/v1/runs/"):
            run_id = path.split("/")[3]
            if path.endswith("/timings"):
                index = self.run_ids.index(run_id)
                attempt = 2
                timing = {
                    "run_id": run_id,
                    "attempt": attempt,
                    "status": "succeeded",
                    "queue_seconds": float(index + 1),
                    "container_lifecycle_seconds": 6.0,
                    "training_evaluation_seconds": 2.0,
                    "report_generation_seconds": 0.5,
                    "total_seconds": float(index + 7.5),
                    "environment": environment(),
                }
                return httpx.Response(200, json=[
                    {**timing, "attempt": 1, "status": "failed"}, timing,
                ])
            if path.endswith("/report"):
                if self.missing_report:
                    return httpx.Response(404, json={"detail": "missing"})
                index = self.run_ids.index(run_id)
                body = {
                    "id": str(UUID(int=300 + index)),
                    "run_id": run_id,
                    "version": 1,
                    "execution_attempt": 2,
                    "generation_ms": 500,
                    "content_sha256": "a" * 64,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "report_json": {
                        "references": {
                            "experiment_id": "experiment-1",
                            "plan_id": str(UUID(int=20)),
                            "plan_version": 1,
                            "code_version": 1,
                            "code_hash": "code-v1",
                            "execution_attempt": 2,
                        }
                    },
                    "markdown": "report",
                }
                self.active_run = None
                return httpx.Response(200, json=body)
            self.polls[run_id] += 1
            status = "queued" if self.never_terminal or self.polls[run_id] == 1 else "succeeded"
            return httpx.Response(200, json={
                "id": run_id,
                "experiment_id": "experiment-1",
                "plan_id": str(UUID(int=20)),
                "plan_version": 1,
                "code_version": 1,
                "status": status,
                "repair_attempt": 1,
            })
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})


def fake_client(server, token="dedicated-local-token"):
    return httpx.Client(
        transport=httpx.MockTransport(server),
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
    )


def test_three_runs_are_sequential_with_fresh_keys_and_latest_attempt_selected():
    measurement = load_measurement()
    server = FakeMeasurementServer()
    token = "dedicated-local-token"
    with fake_client(server, token) as client:
        runs = measurement.measure_three_runs(client, "experiment-1", timeout=10, poll_interval=0)

    assert len(runs) == 3
    assert len(set(server.keys)) == 3
    assert [run["run_id"] for run in runs] == server.run_ids
    assert [run["attempt"] for run in runs] == [2, 2, 2]
    assert [run["queue_seconds"] for run in runs] == [1.0, 2.0, 3.0]
    assert all(request.headers["Authorization"] == f"Bearer {token}" for request in server.requests)
    assert token not in json.dumps(runs)
    post_positions = [
        index for index, request in enumerate(server.requests) if request.method == "POST"
    ]
    assert post_positions[0] < post_positions[1] < post_positions[2]


def test_duplicate_generated_idempotency_keys_fail_before_any_run_request(monkeypatch):
    measurement = load_measurement()
    server = FakeMeasurementServer()
    monkeypatch.setattr(measurement, "uuid4", lambda: UUID(int=999))
    with fake_client(server) as client:
        with pytest.raises(ValueError, match="duplicate idempotency key"):
            measurement.measure_three_runs(client, "experiment-1", timeout=10, poll_interval=0)
    assert not any(request.method == "POST" for request in server.requests)


def test_measurement_rejects_environment_mismatch(sample_runs):
    measurement = load_measurement()
    sample_runs[1]["environment"]["image_digest"] = "sha256:different"
    with pytest.raises(ValueError, match="environment mismatch"):
        measurement.summarize_measurements(sample_runs)


def test_measurement_rejects_resource_budget_mismatch(sample_runs):
    measurement = load_measurement()
    sample_runs[1]["environment"]["resource_budget"]["max_seconds"] = 599
    with pytest.raises(ValueError, match="environment mismatch"):
        measurement.assert_same_environment(sample_runs)


def test_measurement_times_out_without_real_http():
    measurement = load_measurement()
    server = FakeMeasurementServer(never_terminal=True)
    with fake_client(server) as client:
        with pytest.raises(TimeoutError, match="did not reach terminal state"):
            measurement.measure_three_runs(client, "experiment-1", timeout=0, poll_interval=0)


def test_measurement_requires_a_report():
    measurement = load_measurement()
    server = FakeMeasurementServer(missing_report=True)
    with fake_client(server) as client:
        with pytest.raises(RuntimeError, match="missing report"):
            measurement.measure_three_runs(client, "experiment-1", timeout=10, poll_interval=0)


@pytest.mark.parametrize("value", [None, -1, float("inf"), float("nan")])
def test_measurement_rejects_missing_or_invalid_stage(sample_runs, value):
    measurement = load_measurement()
    sample_runs[1]["training_evaluation_seconds"] = value
    with pytest.raises(ValueError, match="training_evaluation_seconds"):
        measurement.summarize_measurements(sample_runs)


def test_summary_emits_min_median_max_raw_values_and_environment_hash(sample_runs):
    measurement = load_measurement()
    original = copy.deepcopy(sample_runs)
    summary = measurement.summarize_measurements(sample_runs)

    assert sample_runs == original
    assert summary["queue_seconds"] == {
        "min": 1.0,
        "median": 2.0,
        "max": 3.0,
        "values": [1.0, 2.0, 3.0],
    }
    assert summary["container_lifecycle_seconds"]["median"] == 6.0
    assert summary["training_evaluation_seconds"]["values"] == [2.0, 2.0, 2.0]
    assert summary["report_generation_seconds"]["values"] == [0.5, 0.5, 0.5]
    assert summary["total_seconds"]["values"] == [7.5, 8.5, 9.5]
    assert summary["environment_hash"] == measurement.environment_hash(environment())
    assert all(math.isfinite(value) for value in summary["total_seconds"]["values"])


def test_write_json_creates_parent_and_never_contains_token(sample_runs, tmp_path: Path):
    measurement = load_measurement()
    output = tmp_path / "acceptance" / "result.json"
    payload = {
        "passed": True,
        "runs": sample_runs,
        "summary": measurement.summarize_measurements(sample_runs),
    }
    measurement.write_json(output, payload)

    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert "dedicated-local-token" not in output.read_text(encoding="utf-8")
