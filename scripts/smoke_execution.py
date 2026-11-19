"""Live Phase-B/C acceptance against the running Compose stack.

Drives the real chain: seed card -> experiment -> confirm/approve plan -> start
run -> Worker codegen -> Runner sibling container (real training) -> metrics +
audit + logs + artifacts + code versions -> auto-generated reproduction report
(14-section Markdown, traceable references, never a Full Reproduction claim).
Also verifies the Runner holds no secrets and has no network egress. Requires
`supabase start` + `docker compose up` + the baked exec image.

Usage: uv run python scripts/smoke_execution.py
"""

import re
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings

from supabase import ClientOptions, create_client

API_URL = "http://localhost:8000"
TERMINAL = {"succeeded", "failed", "timeout", "cancelled"}
failures: list[str] = []


def _explicit(value, page=8):
    return {
        "value": value,
        "status": "paper_explicit",
        "evidence_quote": f"...{value}...",
        "source_page": page,
    }


CARD = {
    "method": _explicit("LeNet-5 gradient-based learning"),
    "model_architecture": _explicit("LeNet-5 convolutional network"),
    "dataset": _explicit("MNIST handwritten digits"),
    "optimizer": _explicit("SGD"),
    "loss_function": _explicit("cross entropy"),
    "hyperparameters": {
        "learning_rate": _explicit("0.01"),
        "batch_size": _explicit(64, 9),
        "epochs": _explicit(2, 9),
    },
    "evaluation_metrics": [_explicit("accuracy", 3)],
    "reported_results": [
        {
            "metric": "error rate",
            "value": 0.8,
            "status": "paper_explicit",
            "evidence_quote": "...the error rate on the MNIST test set is 0.8 percent...",
            "source_page": 10,
        }
    ],
}

FINAL_STATUSES = {
    "METHOD_VALIDATED",
    "SCALED_RUN_COMPLETED",
    "PARTIAL_MATCH",
    "DEVIATION_FOUND",
    "INSUFFICIENT_EVIDENCE",
    "EXECUTION_FAILED",
}


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def docker_exec(container: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    settings = ApiSettings()
    admin = make_service_client(settings)
    suffix = uuid4().hex[:10]
    email = f"exec-smoke-{suffix}@example.test"
    user = admin.auth.admin.create_user(
        {"email": email, "password": "test-password-123", "email_confirm": True}
    ).user

    options = ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))
    anon = create_client(settings.supabase_url, settings.supabase_anon_key, options)
    token = anon.auth.sign_in_with_password(
        {"email": email, "password": "test-password-123"}
    ).session.access_token

    project = (
        admin.table("projects")
        .insert({"user_id": user.id, "name": "exec-smoke", "is_default": True})
        .execute()
        .data[0]
    )
    paper = (
        admin.table("papers")
        .insert(
            {
                "project_id": project["id"],
                "title": "LeNet-5 exec smoke",
                "pdf_path": "smoke/lenet.pdf",
                "source": "upload",
                "status": "parsed",
            }
        )
        .execute()
        .data[0]
    )
    admin.table("reproduction_specs").insert(
        {"paper_id": paper["id"], "version": 1, "origin": "extraction", "spec_json": CARD}
    ).execute()

    api = httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=60.0,
    )
    try:
        created = api.post(f"/v1/papers/{paper['id']}/experiments", json={})
        check("create experiment + plan v1", created.status_code == 201, created.text[:200])
        eid = created.json()["id"]

        edit = api.post(
            f"/v1/experiments/{eid}/plans",
            json={
                "base_version": 1,
                "changes": [
                    {"field_path": "training_config.optimizer", "value": "sgd"},
                    {"field_path": "training_config.loss_function", "value": "cross_entropy"},
                    {"field_path": "dataset_config.train_subset_size", "value": 256},
                    {"field_path": "dataset_config.test_subset_size", "value": 128},
                    {"field_path": "training_config.epochs", "value": 1},
                ],
                "change_reason": "exec smoke: confirm assumptions + tiny subset",
            },
        )
        check("confirm + shrink plan -> v2", edit.status_code == 201, edit.text[:200])

        approved = api.post(f"/v1/experiments/{eid}/plans/2/approve")
        check("approve plan v2", approved.status_code == 200, approved.text[:200])

        started = api.post(f"/v1/experiments/{eid}/runs")
        check("start run (202)", started.status_code == 202, started.text[:200])
        run_id = started.json()["run_id"]

        detail = {}
        for _ in range(150):
            detail = api.get(f"/v1/runs/{run_id}").json()
            if detail["status"] in TERMINAL:
                break
            time.sleep(2)
        check(
            "run succeeded in real container",
            detail.get("status") == "succeeded",
            f"status={detail.get('status')} err={detail.get('error')}",
        )
        metrics = detail.get("metrics_json", {})
        check("metrics collected", metrics.get("metric_value") is not None, str(metrics)[:200])
        audit = detail.get("audit_json", {})
        check(
            "audit complete (image/python/dataset/lock/seed)",
            all(
                audit.get(k)
                for k in (
                    "image_digest",
                    "python_version",
                    "dataset_fingerprint",
                    "dependency_lock_hash",
                    "seed",
                )
            ),
            str(audit)[:200],
        )
        logs = api.get(f"/v1/runs/{run_id}/logs").json()
        stdout_len = len(logs.get("stdout", ""))
        check("stdout log persisted", stdout_len > 0, f"{stdout_len} chars")
        artifacts = api.get(f"/v1/runs/{run_id}/artifacts").json()
        names = {a["artifact_type"] for a in artifacts}
        check("artifacts persisted", {"metrics.json", "model.pt"} <= names, str(names))
        files = api.get(f"/v1/runs/{run_id}/files").json()
        check(
            "code version pinned (5 files)",
            len(files) == 5 and all(f["version"] == 1 for f in files),
            f"{len(files)} files",
        )

        # --- Phase C: reproduction report acceptance ---
        report_resp = api.get(f"/v1/runs/{run_id}/report")
        check(
            "report auto-generated on terminal run",
            report_resp.status_code == 200,
            report_resp.text[:200],
        )
        if report_resp.status_code == 200:
            body = report_resp.json()
            report = body["report_json"]
            markdown = body["markdown"]
            final = report["final_status"]
            check(
                "final status in six-state enum (no Full Reproduction)",
                final in FINAL_STATUSES,
                final,
            )
            refs = report["references"]
            check(
                "report references trace to paper/spec/plan/code/run",
                refs["run_id"] == run_id
                and refs["paper_id"] == paper["id"]
                and refs["spec_version"] == 1
                and refs["plan_version"] == 2
                and refs["code_version"] == 1
                and len(refs["generated_file_ids"]) == 5,
                str(refs)[:200],
            )
            acc = next(
                (c for c in report["metric_comparisons"] if c["metric"] == "accuracy"), None
            )
            check(
                "accuracy comparison: real metrics vs grounded paper evidence",
                acc is not None
                and acc["experiment_value"] == metrics.get("accuracy")
                and abs(acc["paper_value"] - 0.992) < 1e-9
                and acc["percentage_point_difference"] is not None,
                str(acc)[:200],
            )
            sections = [int(m) for m in re.findall(r"^## (\d+)\. ", markdown, re.MULTILINE)]
            check("markdown report has 14 ordered sections", sections == list(range(1, 15)))
            check(
                "summary disclaims full reproduction",
                "Full Reproduction" in report["summary"],
            )
            check(
                "scaling deviations recorded (subset/epochs vs paper)",
                len(report["scaling_deviations"]) > 0,
                f"{len(report['scaling_deviations'])} deviations",
            )
            regen = api.post(f"/v1/runs/{run_id}/report")
            check(
                "manual report regeneration (upsert)",
                regen.status_code == 200 and regen.json()["run_id"] == run_id,
                regen.text[:200],
            )
    finally:
        api.close()
        try:
            admin.auth.admin.delete_user(user.id)
        except Exception:
            pass

    env = docker_exec("reproagent-runner-1", "env")
    check(
        "runner holds no secrets",
        not any(s in env.stdout for s in ("QWEN_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "sk-")),
    )
    egress = docker_exec(
        "reproagent-runner-1",
        "python",
        "-c",
        "import socket; socket.create_connection(('example.com',80),timeout=4); print('CONNECTED')",
    )
    check("runner has no network egress", "CONNECTED" not in egress.stdout)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nAll execution acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())