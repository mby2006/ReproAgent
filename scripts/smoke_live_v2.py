"""Controlled HTTP support gate against the real local full stack.

Usage: LIVE_API_TOKEN=... uv run python scripts/smoke_live_v2.py --base-url ... --output ...
The token is read from LIVE_API_TOKEN and is never printed or persisted.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pymupdf
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings


def poll(client: httpx.Client, workflow_id: str, targets: set[str], timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/workflows/{workflow_id}")
        response.raise_for_status()
        workflow = response.json()
        if workflow["status"] in targets:
            return workflow
        time.sleep(2)
    raise TimeoutError(f"workflow {workflow_id} did not reach {sorted(targets)}")


def upload(client: httpx.Client, path: Path) -> dict:
    with path.open("rb") as stream:
        response = client.post(
            "/v1/papers",
            files={"file": (path.name, stream, "application/pdf")},
            data={"title": "v2 support gate"},
        )
    response.raise_for_status()
    if response.status_code != 202:
        raise RuntimeError("paper upload was not accepted")
    return response.json()


def write_scanned_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    pixmap = page.get_pixmap(colorspace=pymupdf.csGRAY)
    scanned = pymupdf.open()
    scanned.new_page(width=612, height=792).insert_image(
        pymupdf.Rect(0, 0, 612, 792), pixmap=pixmap
    )
    scanned.save(path)
    scanned.close()
    document.close()


def trusted_counts(workflow_id: str) -> tuple[int, int]:
    """Count workflow and paper-linked Runs plus their Runner dispatch intents."""
    admin = make_service_client(ApiSettings())
    workflow = (
        admin.table("workflows")
        .select("run_id,paper_id")
        .eq("id", workflow_id)
        .single()
        .execute()
        .data
    )
    run_ids = {workflow["run_id"]} if workflow.get("run_id") else set()
    paper_id = workflow.get("paper_id")
    if paper_id:
        experiments = (
            admin.table("experiments").select("id").eq("paper_id", paper_id).execute().data
        )
        experiment_ids = [row["id"] for row in experiments]
        if experiment_ids:
            runs = (
                admin.table("execution_runs")
                .select("id")
                .in_("experiment_id", experiment_ids)
                .execute()
                .data
            )
            run_ids.update(row["id"] for row in runs)
    if not run_ids:
        return 0, 0
    outbox_count = len(
        admin.table("job_outbox")
        .select("id")
        .in_("entity_id", sorted(run_ids))
        .eq("job_type", "run_experiment_task")
        .execute()
        .data
    )
    return len(run_ids), outbox_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--lenet-pdf", default=str(Path(__file__).resolve().parents[1] / "docs/1998-lecun.pdf")
    )
    parser.add_argument("--agent-url", default="http://127.0.0.1:8001")
    parser.add_argument("--timeout", type=int, default=1800)
    token = os.environ.pop("LIVE_API_TOKEN", None)
    args = parser.parse_args()
    if not token:
        parser.error("LIVE_API_TOKEN is required")
    args.token = token
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    scanned_pdf = output / "scanned-input.pdf"
    write_scanned_pdf(scanned_pdf)
    gate: dict = {"scanned": {}, "lenet": {}, "passed": False}
    try:
        with (
            httpx.Client(
                base_url=args.base_url,
                headers={"Authorization": f"Bearer {args.token}"},
                timeout=180,
                trust_env=False,
            ) as client,
            httpx.Client(base_url=args.agent_url, timeout=300, trust_env=False) as agent,
        ):
            scanned = upload(client, scanned_pdf)
            workflow = poll(client, scanned["workflow_id"], {"failed"}, args.timeout)
            error = workflow.get("error") or ""
            error_code = workflow.get("error_code") or error.split(":", 1)[0]
            run_count, outbox_count = trusted_counts(scanned["workflow_id"])
            gate["scanned"] = {
                "paper_id": scanned["paper_id"],
                "workflow_id": scanned["workflow_id"],
                "status": workflow["status"],
                "error_code": error_code,
                "execution_run_count": run_count,
                "runner_outbox_count": outbox_count,
            }
            if (
                error_code != "SCANNED_PDF"
                or not error.startswith("SCANNED_PDF:")
                or run_count
                or outbox_count
            ):
                raise RuntimeError("scanned PDF did not fail closed with zero Runner submissions")

            lenet = upload(client, Path(args.lenet_pdf))
            workflow = poll(
                client, lenet["workflow_id"], {"awaiting_spec_review", "failed"}, args.timeout
            )
            gate["lenet"] = {
                "paper_id": lenet["paper_id"],
                "workflow_id": lenet["workflow_id"],
                "status": workflow["status"],
            }
            if workflow["status"] != "awaiting_spec_review":
                raise RuntimeError("LeNet workflow did not reach spec review")
            card = client.get(f"/v1/papers/{lenet['paper_id']}/card").json()["card"]
            parse = agent.post(
                "/v1/parse",
                json={"pdf_b64": base64.b64encode(Path(args.lenet_pdf).read_bytes()).decode()},
            )
            parse.raise_for_status()
            support = agent.post(
                "/v1/papers/support-check",
                json={"document": parse.json()["document"], "card": card},
            )
            support.raise_for_status()
            body = support.json()
            gate["lenet"].update({"support_code": body["code"], "supported": body["supported"]})
            if body["code"] != "SUPPORTED" or not body["supported"]:
                raise RuntimeError("Agent support-check did not return SUPPORTED")
        gate["passed"] = True
        return 0
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            code = "WORKFLOW_TIMEOUT"
        elif isinstance(exc, httpx.HTTPStatusError):
            code = f"HTTP_{exc.response.status_code}"
        elif "scanned PDF" in str(exc):
            code = "SCANNED_PDF_GATE_FAILED"
        elif "support-check" in str(exc):
            code = "SUPPORT_CHECK_FAILED"
        else:
            code = "SMOKE_FAILED"
        gate["error_code"] = code
        return 1
    finally:
        (output / "support-gate.json").write_text(
            json.dumps(gate, indent=2) + "\n", encoding="utf-8"
        )
        scanned_pdf.unlink(missing_ok=True)
        args.token = ""


if __name__ == "__main__":
    sys.exit(main())
