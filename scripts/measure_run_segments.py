"""Measure three sequential Runs in one approved, identical environment.

The dedicated acceptance token is read only to construct the Authorization
header. It is never included in the result document or diagnostic output.
"""

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from statistics import median
from uuid import uuid4

import httpx

TERMINAL_STATUSES = {"succeeded", "failed", "timeout", "cancelled"}
STAGE_FIELDS = (
    "queue_seconds",
    "container_lifecycle_seconds",
    "training_evaluation_seconds",
    "report_generation_seconds",
    "total_seconds",
)
ENVIRONMENT_FIELDS = (
    "image_digest",
    "dataset_fingerprint",
    "dependency_lock_hash",
    "plan_hash",
    "code_hash",
    "seed",
    "resource_budget",
)
RESOURCE_BUDGET_FIELDS = (
    "max_seconds",
    "memory_mb",
    "cpu_count",
    "pids_limit",
    "disk_mb",
    "device",
)


def _response_json(response: httpx.Response) -> dict | list:
    response.raise_for_status()
    return response.json()


def _approved_plan(client: httpx.Client, experiment_id: str) -> tuple[dict, dict]:
    experiment = _response_json(client.get(f"/v1/experiments/{experiment_id}"))
    if experiment.get("id") != experiment_id:
        raise ValueError("experiment mismatch")
    plan = experiment.get("plan")
    if not isinstance(plan, dict) or not plan.get("approved_at"):
        raise ValueError("experiment has no approved plan")
    return experiment, plan


def _create_and_wait(
    client: httpx.Client,
    experiment_id: str,
    plan: dict,
    idempotency_key: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict:
    accepted = _response_json(client.post(
        f"/v1/experiments/{experiment_id}/runs",
        headers={"Idempotency-Key": idempotency_key},
    ))
    run_id = accepted.get("run_id")
    if not run_id:
        raise RuntimeError("run creation returned no run_id")
    deadline = time.monotonic() + timeout
    while True:
        run = _response_json(client.get(f"/v1/runs/{run_id}"))
        if run.get("id") != run_id:
            raise ValueError("run identity mismatch")
        if run.get("experiment_id") != experiment_id:
            raise ValueError("experiment mismatch")
        if run.get("plan_id") != plan.get("id") or run.get("plan_version") != plan.get("version"):
            raise ValueError("approved plan mismatch")
        status = run.get("status")
        if status in TERMINAL_STATUSES:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"run {run_id} did not reach terminal state before timeout")
        if poll_interval > 0:
            time.sleep(poll_interval)
    if status != "succeeded":
        raise RuntimeError(f"run {run_id} did not succeed: {status}")

    timings = _response_json(client.get(f"/v1/runs/{run_id}/timings"))
    if not isinstance(timings, list) or not timings:
        raise RuntimeError(f"run {run_id} has no timing evidence")
    latest = max(timings, key=lambda row: row.get("attempt", 0))
    if latest.get("run_id") != run_id or latest.get("attempt", 0) < 1:
        raise ValueError("timing attempt identity mismatch")
    if latest.get("status") != status:
        raise ValueError("timing status mismatch")
    if "repair_attempt" in run and latest["attempt"] != run["repair_attempt"] + 1:
        raise ValueError("timing attempt is not the latest run attempt")
    try:
        report_response = client.get(f"/v1/runs/{run_id}/report")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise RuntimeError(f"missing report for run {run_id}") from exc
        raise
    if report_response.status_code == 404:
        raise RuntimeError(f"missing report for run {run_id}")
    report = _response_json(report_response)
    attempt = latest["attempt"]
    if report.get("execution_attempt", report.get("attempt")) != attempt:
        raise ValueError("report attempt mismatch")
    references = report.get("report_json", {}).get("references", {})
    if references.get("experiment_id") != experiment_id:
        raise ValueError("report experiment mismatch")
    if (
        references.get("plan_id") != plan.get("id")
        or references.get("plan_version") != plan.get("version")
    ):
        raise ValueError("report approved plan mismatch")
    if references.get("code_version") != run.get("code_version"):
        raise ValueError("report code version mismatch")
    environment = latest.get("environment")
    if (
        not isinstance(environment, dict)
        or references.get("code_hash") != environment.get("code_hash")
    ):
        raise ValueError("report code hash mismatch")
    return {
        "run_id": run_id,
        "experiment_id": experiment_id,
        "plan_id": run.get("plan_id"),
        "plan_version": run.get("plan_version"),
        "code_version": run.get("code_version"),
        "attempt": attempt,
        "status": status,
        "queue_seconds": latest.get("queue_seconds"),
        "container_lifecycle_seconds": latest.get("container_lifecycle_seconds"),
        "training_evaluation_seconds": latest.get("training_evaluation_seconds"),
        "report_generation_seconds": latest.get("report_generation_seconds"),
        "total_seconds": latest.get("total_seconds"),
        "environment": environment,
        "report": report,
    }


def measure_three_runs(
    client: httpx.Client,
    experiment_id: str,
    *,
    timeout: float = 900,
    poll_interval: float = 2,
) -> list[dict]:
    """Create and fully observe each Run before creating the next one."""
    keys = [f"nfr05-{uuid4()}" for _ in range(3)]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate idempotency key generated")
    _, plan = _approved_plan(client, experiment_id)
    runs = [
        _create_and_wait(
            client,
            experiment_id,
            plan,
            key,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        for key in keys
    ]
    assert_same_environment(runs)
    code_versions = {run["code_version"] for run in runs}
    if len(code_versions) != 1:
        raise ValueError("code version mismatch")
    return runs


def assert_same_environment(runs: list[dict]) -> None:
    if len(runs) != 3:
        raise ValueError("exactly three runs are required")
    first = runs[0].get("environment")
    if not isinstance(first, dict) or any(first.get(field) is None for field in ENVIRONMENT_FIELDS):
        raise ValueError("environment mismatch: incomplete identity")
    budget = first.get("resource_budget")
    if not isinstance(budget, dict) or any(
        budget.get(field) is None for field in RESOURCE_BUDGET_FIELDS
    ):
        raise ValueError("environment mismatch: incomplete resource budget")
    for run in runs[1:]:
        if run.get("experiment_id") != runs[0].get("experiment_id"):
            raise ValueError("experiment mismatch")
        if (
            run.get("plan_id") != runs[0].get("plan_id")
            or run.get("plan_version") != runs[0].get("plan_version")
        ):
            raise ValueError("approved plan mismatch")
        if run.get("code_version") != runs[0].get("code_version"):
            raise ValueError("code version mismatch")
        if run.get("environment") != first:
            raise ValueError("environment mismatch")


def _finite_nonnegative(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be finite and nonnegative")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value


def summarize_measurements(runs: list[dict]) -> dict:
    assert_same_environment(runs)
    summary = {}
    for field in STAGE_FIELDS:
        values = [_finite_nonnegative(run.get(field), field) for run in runs]
        summary[field] = {
            "min": min(values),
            "median": median(values),
            "max": max(values),
            "values": values,
        }
    for run in runs:
        expected_total = (
            run["queue_seconds"]
            + run["container_lifecycle_seconds"]
            + run["report_generation_seconds"]
        )
        if run["total_seconds"] != expected_total:
            raise ValueError(
                "total_seconds must equal queue + container lifecycle + report generation"
            )
    summary["environment_hash"] = environment_hash(runs[0]["environment"])
    return summary


def environment_hash(environment: dict) -> str:
    canonical = json.dumps(
        environment, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_json(path: str | Path, payload: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--runs", type=int, choices=[3], default=3)
    parser.add_argument(
        "--output",
        default="_bmad-output/acceptance/v2/nfr05/result.json",
    )
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    token = os.environ.get("REPROAGENT_ACCESS_TOKEN")
    if not token:
        parser.error("REPROAGENT_ACCESS_TOKEN is required")
    if args.timeout < 0 or args.poll_interval < 0:
        parser.error("timeout and poll interval must be nonnegative")
    try:
        with httpx.Client(
            base_url=args.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(args.timeout),
            trust_env=False,
        ) as client:
            runs = measure_three_runs(
                client,
                args.experiment_id,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
            )
        summary = summarize_measurements(runs)
        write_json(args.output, {"passed": True, "runs": runs, "summary": summary})
    except Exception as exc:
        parser.exit(1, f"measurement failed: {exc}\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
