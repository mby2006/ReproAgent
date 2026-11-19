"""Live Phase-D acceptance: the unified LangGraph workflow, end to end, from the API.

Drives the REAL running Compose stack (real Qwen extraction + real arq worker +
Postgres checkpointer + real Runner sibling container): upload the 47-page LeNet-5
PDF -> parse/extract/ground -> spec gate -> confirm card -> resume(spec) -> plan
gate -> confirm assumptions + shrink subset -> approve -> resume(plan) -> codegen
-> validate -> REAL MNIST training -> metrics -> evaluation report -> persisted,
traceable results. Verifies budget accounting and credential-redacted traces.

Requires `supabase start` + `docker compose up` (rebuilt api/agent images) + the
baked exec image + a real QWEN_API_KEY in .env.

Usage: uv run python scripts/smoke_workflow.py
"""

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings

from supabase import ClientOptions, create_client

API_URL = "http://localhost:8000"
PDF_PATH = Path(__file__).resolve().parents[1] / "docs" / "1998-lecun.pdf"
AWAITING_SPEC = "awaiting_spec_review"
AWAITING_PLAN = "awaiting_plan_approval"
TERMINAL_WF = {"completed", "failed"}
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def poll_workflow(api, workflow_id: str, targets: set[str], tries: int = 300) -> dict:
    wf = {}
    for _ in range(tries):
        try:
            wf = api.get(f"/v1/workflows/{workflow_id}").json()
        except httpx.HTTPError:
            time.sleep(2)
            continue
        if wf.get("status") in targets:
            return wf
        time.sleep(2)
    return wf


def main() -> int:
    settings = ApiSettings()
    admin = make_service_client(settings)
    suffix = uuid4().hex[:10]
    email = f"wf-smoke-{suffix}@example.test"
    user = admin.auth.admin.create_user(
        {"email": email, "password": "test-password-123", "email_confirm": True}
    ).user
    options = ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))
    anon = create_client(settings.supabase_url, settings.supabase_anon_key, options)
    token = anon.auth.sign_in_with_password(
        {"email": email, "password": "test-password-123"}
    ).session.access_token
    api = httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=120.0,
    )

    try:
        # 1. Upload the real paper -> workflow queued.
        with PDF_PATH.open("rb") as fh:
            upload = api.post(
                "/v1/papers",
                files={"file": ("1998-lecun.pdf", fh, "application/pdf")},
                data={"title": "LeNet-5 (live workflow smoke)"},
            )
        check("upload paper -> workflow (202)", upload.status_code == 202, upload.text[:200])
        ids = upload.json()
        paper_id, workflow_id = ids["paper_id"], ids["workflow_id"]

        # 2. Real Qwen extraction + grounding -> spec gate (may take minutes).
        wf = poll_workflow(api, workflow_id, {AWAITING_SPEC, "failed"})
        check("reach awaiting_spec_review", wf.get("status") == AWAITING_SPEC, str(wf)[:200])
        if wf.get("status") != AWAITING_SPEC:
            return 1
        card = api.get(f"/v1/papers/{paper_id}/card").json()["card"]
        fields = [card.get(k) or {} for k in
                  ("method", "model_architecture", "dataset", "optimizer", "loss_function")]
        fields += list(card.get("hyperparameters", {}).values())
        fields += card.get("evaluation_metrics", []) + card.get("reported_results", [])
        grounded = [f for f in fields if f.get("source_page") is not None]
        check(
            "evidence grounded with real page numbers",
            len(grounded) > 0,
            f"{len(grounded)} grounded fields, e.g. {str(grounded[0])[:120]}" if grounded
            else str(card.get("dataset"))[:160],
        )

        # 3. Confirm missing info, then confirm the card.
        api.post(
            f"/v1/papers/{paper_id}/card/edits",
            json={
                "base_version": card_version(api, paper_id),
                "edits": [
                    {"field_path": "loss_function", "value": "cross entropy"},
                    {"field_path": "optimizer", "value": "sgd"},
                ],
            },
        )
        confirm = api.post(f"/v1/papers/{paper_id}/card/confirm")
        check("confirm card (201)", confirm.status_code == 201, confirm.text[:200])

        # 4. Resume past spec gate -> real plan -> plan gate.
        resume_spec = api.post(f"/v1/workflows/{workflow_id}/resume", json={"stage": "spec"})
        check(
            "resume(spec) accepted", resume_spec.status_code in (200, 202), resume_spec.text[:200]
        )
        wf = poll_workflow(api, workflow_id, {AWAITING_PLAN, "failed"})
        check("reach awaiting_plan_approval", wf.get("status") == AWAITING_PLAN, str(wf)[:200])
        if wf.get("status") != AWAITING_PLAN:
            return 1
        eid = wf["experiment_id"]

        # 5. Confirm required assumptions + shrink to a tiny subset, then approve.
        plan = api.get(f"/v1/experiments/{eid}/plans/1").json()["plan"]
        changes = [
            {"field_path": "dataset_config.train_subset_size", "value": 256},
            {"field_path": "dataset_config.test_subset_size", "value": 128},
            {"field_path": "training_config.epochs", "value": 1},
        ]
        seen = {c["field_path"] for c in changes}
        for a in plan["assumptions"]:
            fp = a["field_path"]
            if a["requires_confirmation"] and not a["human_confirmed"] and fp not in seen:
                changes.append({"field_path": fp, "value": a["value"]})
                seen.add(fp)
        edit = api.post(
            f"/v1/experiments/{eid}/plans",
            json={"base_version": 1, "changes": changes, "change_reason": "live smoke tiny subset"},
        )
        check("confirm + shrink plan -> v2", edit.status_code == 201, edit.text[:200])
        approve = api.post(f"/v1/experiments/{eid}/plans/2/approve")
        check("approve plan v2", approve.status_code == 200, approve.text[:200])

        # 6-9. Resume past plan gate -> real container training -> report.
        resume_plan = api.post(f"/v1/workflows/{workflow_id}/resume", json={"stage": "plan"})
        check(
            "resume(plan) accepted", resume_plan.status_code in (200, 202), resume_plan.text[:200]
        )
        wf = poll_workflow(api, workflow_id, TERMINAL_WF, tries=200)
        check("workflow completed", wf.get("status") == "completed", str(wf)[:240])
        if wf.get("status") != "completed":
            return 1
        run_id = wf["run_id"]

        run = api.get(f"/v1/runs/{run_id}").json()
        check("real run succeeded", run.get("status") == "succeeded", str(run.get("error"))[:200])
        metrics = run.get("metrics_json", {})
        check("real metrics collected", metrics.get("metric_value") is not None, str(metrics)[:160])
        check("audit complete", bool(run.get("audit_json", {}).get("dataset_fingerprint")))

        # 10. Report linked to the real execution record.
        report = api.get(f"/v1/runs/{run_id}/report").json()
        refs = report["report_json"]["references"]
        check("report references real run", refs["run_id"] == run_id, str(refs)[:160])
        check("report references code hash", refs["code_hash"] == run["code_hash"])
        check("14-section markdown", report["markdown"].count("\n## ") >= 10)

        budget = wf.get("budget_json", {})
        check("budget recorded LLM + experiment", budget.get("llm_calls", 0) >= 1
              and budget.get("experiment_seconds", 0) > 0, str(budget)[:160])

        traces = api.get(f"/v1/workflows/{workflow_id}/traces").json()
        nodes = {t["node_name"] for t in traces}
        check(
            "key nodes traced",
            {"execute_code", "evaluate_result", "generate_report", "persist_results"} <= nodes,
            str(sorted(nodes))[:200],
        )
        blob = json.dumps(traces)
        check(
            "no credentials in traces",
            all(s not in blob for s in ("sk-", "pdf_b64", "service_role")),
        )
    finally:
        api.close()
        admin.auth.admin.delete_user(user.id)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL LIVE WORKFLOW SMOKE CHECKS PASSED")
    return 0


def card_version(api, paper_id: str) -> int:
    return api.get(f"/v1/papers/{paper_id}/card").json()["version"]


if __name__ == "__main__":
    sys.exit(main())