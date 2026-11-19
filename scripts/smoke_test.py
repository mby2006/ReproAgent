"""End-to-end smoke test against the running Docker Compose stack.

Usage: uv run python scripts/smoke_test.py
Checks web/api/agent directly; runner is verified through the agent chain
because it sits on an isolated network with no published ports.
"""

import sys

import httpx

WEB_URL = "http://localhost:3000"
API_URL = "http://localhost:8000"
AGENT_URL = "http://localhost:8001"

failures: list[str] = []


def _explicit(value, page=8):
    return {
        "value": value,
        "status": "paper_explicit",
        "evidence_quote": f"...{value}...",
        "source_page": page,
    }


LENET_CARD = {
    "method": _explicit("LeNet-5 gradient-based learning"),
    "model_architecture": _explicit("LeNet-5 convolutional network"),
    "dataset": _explicit("MNIST handwritten digits"),
    "optimizer": _explicit("stochastic diagonal Levenberg-Marquardt", 4),
    "loss_function": {"value": None, "status": "missing"},
    "hyperparameters": {
        "learning_rate": _explicit("0.001", 4),
        "batch_size": _explicit(128, 4),
        "epochs": _explicit(20, 4),
    },
    "evaluation_metrics": [_explicit("accuracy", 3)],
}


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        try:
            response = client.get(WEB_URL)
            check("web reachable", response.status_code == 200, f"HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            check("web reachable", False, str(exc))

        try:
            body = client.get(f"{API_URL}/health").json()
            check("api /health", body.get("status") == "ok", str(body))
        except httpx.HTTPError as exc:
            check("api /health", False, str(exc))

        try:
            body = client.get(f"{AGENT_URL}/health").json()
            check(
                "agent /health graph loaded",
                body.get("status") == "ok" and body["details"]["graph_loaded"] is True,
                str(body.get("details")),
            )
        except httpx.HTTPError as exc:
            check("agent /health graph loaded", False, str(exc))

        try:
            body = client.get(f"{AGENT_URL}/health/dependencies").json()
            runner = next(d for d in body["dependencies"] if d["name"] == "runner")
            check("runner healthy via agent", runner["status"] == "ok", str(runner))
        except (httpx.HTTPError, StopIteration) as exc:
            check("runner healthy via agent", False, str(exc))

        try:
            body = client.get(f"{API_URL}/health/dependencies").json()
            deps = {d["name"]: d["status"] for d in body["dependencies"]}
            check(
                "api dependencies all ok (agent/supabase/runner)",
                all(status == "ok" for status in deps.values()),
                str(deps),
            )
        except httpx.HTTPError as exc:
            check("api dependencies all ok (agent/supabase/runner)", False, str(exc))

        try:
            response = client.post(f"{AGENT_URL}/v1/plan", json={"card": LENET_CARD})
            plan = response.json()["plan"]
            unconfirmed = [
                a["field_path"]
                for a in plan["assumptions"]
                if a["requires_confirmation"] and not a["human_confirmed"]
            ]
            check(
                "agent /v1/plan deterministic LeNet-5 plan",
                response.status_code == 200
                and plan["model_config"]["architecture"] == "lenet-5"
                and plan["training_config"]["batch_size"]["source"] == "mvp_scaling"
                and plan["training_config"]["learning_rate"]["source"] == "paper_explicit"
                and "training_config.optimizer" in unconfirmed
                and "training_config.loss_function" in unconfirmed,
                f"HTTP {response.status_code}, unconfirmed={unconfirmed}",
            )
        except (httpx.HTTPError, KeyError) as exc:
            check("agent /v1/plan deterministic LeNet-5 plan", False, str(exc))

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
