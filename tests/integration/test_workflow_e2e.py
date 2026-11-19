"""Integration: the FULL unified-workflow closed loop, driven through the HTTP API.

Upload LeNet-5 PDF -> real parse -> stub-LLM card -> real evidence grounding ->
user confirms missing info -> resume(spec) -> real deterministic plan -> user
confirms assumptions + scales to a tiny subset -> resume(plan) -> real codegen ->
real static validation -> REAL MNIST training in a locked-down Docker container ->
real metrics -> real evaluation report -> persisted, traceable results.

The LLM extraction step is stubbed at the HTTP boundary; every other agent step
runs the real deterministic engine in-process, and execution spawns a REAL
sibling container via the Runner app (ASGI transport). State is checkpointed to a
shared memory saver so the two human gates resume the same graph thread.

Requires `supabase start` + .env local keys + the workflow migration, a running
Docker daemon and the baked `reproagent-exec:latest` image. Run explicitly:
    uv run pytest tests/integration/test_workflow_e2e.py -q
"""

import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from reproagent_agent.codegen import (
    classify_error,
    generate_from_plan,
    repair_bundle,
    validate_bundle,
)
from reproagent_agent.evaluation import evaluate_run
from reproagent_agent.planning import build_plan
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.worker import resume_workflow_task, run_workflow_task
from reproagent_api.workflow import make_memory_checkpointer
from reproagent_paperparse import ground_card, parse_pdf
from reproagent_runner.config import RunnerSettings
from reproagent_runner.main import create_app as create_runner_app
from reproagent_runner.registry import RunRegistry
from reproagent_runner.sandbox import make_client
from reproagent_schemas import (
    ClassifyRequest,
    CodeBundle,
    CodegenResponse,
    EvaluateRequest,
    ExperimentPlan,
    ExtractResponse,
    FinalStatus,
    GroundResponse,
    LLMUsage,
    ParsedDocument,
    ParseResponse,
    PlanResponse,
    RepairRequest,
    ReproductionCard,
    RunStatus,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_understanding import (  # noqa: E402
    MOCK_CARD,
    FakeArq,
    build_pdf,
    token_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = REPO_ROOT / ".runner-work-e2e"


def full_agent_transport() -> httpx.MockTransport:
    """Real deterministic agent engines; only the LLM extraction is stubbed."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        if path == "/v1/parse":
            document = parse_pdf(base64.b64decode(body["pdf_b64"]))
            payload = ParseResponse(document=document).model_dump(mode="json")
        elif path == "/v1/extract":
            usage = LLMUsage(
                model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
            )
            payload = ExtractResponse(card_raw=MOCK_CARD, usages=[usage]).model_dump(mode="json")
        elif path == "/v1/ground":
            doc = ParsedDocument.model_validate(body["document"])
            card = ReproductionCard.model_validate(ground_card(body["card_raw"], doc))
            payload = GroundResponse(card=card).model_dump(mode="json")
        elif path == "/v1/plan":
            card = ReproductionCard.model_validate(body["card"])
            payload = PlanResponse(plan=build_plan(card)).model_dump(mode="json")
        elif path == "/v1/codegen":
            plan = ExperimentPlan.model_validate(body["plan"])
            payload = CodegenResponse(bundle=generate_from_plan(plan)).model_dump(mode="json")
        elif path == "/v1/codegen/validate":
            payload = validate_bundle(CodeBundle.model_validate(body["bundle"])).model_dump(
                mode="json"
            )
        elif path == "/v1/codegen/classify":
            payload = classify_error(ClassifyRequest.model_validate(body)).model_dump(mode="json")
        elif path == "/v1/codegen/repair":
            payload = repair_bundle(RepairRequest.model_validate(body)).model_dump(mode="json")
        elif path == "/v1/evaluate":
            payload = evaluate_run(EvaluateRequest.model_validate(body)).model_dump(mode="json")
        else:
            return httpx.Response(404, json={"detail": f"unexpected path {path}"})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def settings() -> ApiSettings:
    return ApiSettings()


@pytest.fixture(scope="module")
def admin(settings):
    return make_service_client(settings)


@pytest.fixture(scope="module")
def runner(settings):
    """Real Runner app + Docker client; skip when Docker/image unavailable."""
    runner_settings = RunnerSettings(work_dir=WORK_DIR, enforce_disk_quota=False, _env_file=None)
    try:
        client = make_client(runner_settings)
        client.ping()
        client.images.get(runner_settings.exec_image)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker daemon or exec image unavailable: {exc}")
    app = create_runner_app(runner_settings, client=client)
    # ASGITransport does not run lifespan events, so wire the registry by hand.
    app.state.registry = RunRegistry(runner_settings, client=client)
    return httpx.ASGITransport(app=app)


@pytest.fixture(scope="module")
def two_users(admin):
    suffix = uuid4().hex[:12]
    users = []
    for name in ("alice", "bob"):
        created = admin.auth.admin.create_user(
            {
                "email": f"{name}-e2e-{suffix}@example.test",
                "password": "test-password-123",
                "email_confirm": True,
            }
        )
        users.append(created.user)
    yield users
    for user in users:
        admin.auth.admin.delete_user(user.id)


@pytest.fixture(scope="module")
def alice_client(settings, two_users):
    alice, _ = two_users
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.arq = FakeArq()
        client.headers["Authorization"] = f"Bearer {token_for(settings, alice.email)}"
        yield client


async def drive(settings, runner, checkpointer, workflow_id: str, stage: str | None = None):
    ctx = {
        "settings": settings,
        "agent_transport": full_agent_transport(),
        "runner_transport": runner,
        "checkpointer": checkpointer,
    }
    if stage is None:
        await run_workflow_task(ctx, workflow_id)
    else:
        await resume_workflow_task(ctx, workflow_id, stage)


def get_workflow(client, workflow_id: str) -> dict:
    response = client.get(f"/v1/workflows/{workflow_id}")
    assert response.status_code == 200, response.text
    return response.json()


class TestFullClosedLoop:
    async def test_upload_to_report_with_real_training(self, settings, alice_client, runner):
        checkpointer = make_memory_checkpointer()

        # 1. Upload the LeNet-5 paper -> a workflow is created and queued.
        upload = alice_client.post(
            "/v1/papers",
            files={"file": ("lenet.pdf", build_pdf(), "application/pdf")},
            data={"title": "LeNet-5 paper"},
        )
        assert upload.status_code == 202, upload.text
        ids = upload.json()
        paper_id, workflow_id = ids["paper_id"], ids["workflow_id"]
        assert get_workflow(alice_client, workflow_id)["status"] == "uploaded"

        # 2. Drive the graph: real parse + stub-LLM card + real grounding -> spec gate.
        await drive(settings, runner, checkpointer, workflow_id)
        wf = get_workflow(alice_client, workflow_id)
        assert wf["status"] == "awaiting_spec_review", wf
        assert wf["spec_version_id"] is not None
        card = alice_client.get(f"/v1/papers/{paper_id}/card").json()
        assert card["card"]["loss_function"]["status"] == "missing"
        assert card["card"]["hyperparameters"]["learning_rate"]["source_page"] == 2

        # 3. User confirms the missing information (loss function + optimizer).
        edit = alice_client.post(
            f"/v1/papers/{paper_id}/card/edits",
            json={
                "base_version": 1,
                "edits": [
                    {"field_path": "loss_function", "value": "cross entropy"},
                    {"field_path": "optimizer", "value": "sgd"},
                ],
            },
        )
        assert edit.status_code == 201, edit.text
        confirm = alice_client.post(f"/v1/papers/{paper_id}/card/confirm")
        assert confirm.status_code == 201, confirm.text
        assert confirm.json()["card"]["loss_function"]["human_confirmed"] is True

        # 4. Resume past the spec gate -> real deterministic plan -> plan gate.
        await drive(settings, runner, checkpointer, workflow_id, stage="spec")
        wf = get_workflow(alice_client, workflow_id)
        assert wf["status"] == "awaiting_plan_approval", wf
        experiment_id = wf["experiment_id"]
        assert experiment_id is not None

        # 5. User confirms every required assumption and scales to a tiny subset.
        plan_v1 = alice_client.get(f"/v1/experiments/{experiment_id}/plans/1").json()["plan"]
        changes = [
            {"field_path": "dataset_config.train_subset_size", "value": 256},
            {"field_path": "dataset_config.test_subset_size", "value": 128},
            {"field_path": "training_config.epochs", "value": 1},
        ]
        seen = {c["field_path"] for c in changes}
        for assumption in plan_v1["assumptions"]:
            fp = assumption["field_path"]
            if assumption["requires_confirmation"] and not assumption["human_confirmed"]:
                if fp not in seen:
                    changes.append({"field_path": fp, "value": assumption["value"]})
                    seen.add(fp)
        edit_plan = alice_client.post(
            f"/v1/experiments/{experiment_id}/plans",
            json={"base_version": 1, "changes": changes, "change_reason": "e2e tiny subset"},
        )
        assert edit_plan.status_code == 201, edit_plan.text
        assert edit_plan.json()["version"] == 2
        approve = alice_client.post(f"/v1/experiments/{experiment_id}/plans/2/approve")
        assert approve.status_code == 200, approve.text
        assert approve.json()["approved_at"] is not None

        # 6-9. Resume past the plan gate -> codegen -> validate -> REAL container
        # training -> metrics -> evaluation -> report -> persist.
        await drive(settings, runner, checkpointer, workflow_id, stage="plan")
        wf = get_workflow(alice_client, workflow_id)
        assert wf["status"] == "completed", wf
        assert wf["error"] is None
        run_id = wf["run_id"]
        assert run_id is not None

        # The run really executed and produced genuine metrics + audit.
        run = alice_client.get(f"/v1/runs/{run_id}").json()
        assert run["status"] == RunStatus.SUCCEEDED.value, run
        target_metric = run["metrics_json"]["target_metric"]
        assert target_metric in ("accuracy", "error_rate")
        assert isinstance(run["metrics_json"]["metric_value"], (int, float))
        assert run["audit_json"]["seed"] == 42
        assert run["audit_json"]["dataset_fingerprint"]

        # 10. The report is linked to the real execution record.
        report_resp = alice_client.get(f"/v1/runs/{run_id}/report")
        assert report_resp.status_code == 200, report_resp.text
        report = report_resp.json()["report_json"]
        assert report["references"]["run_id"] == run_id
        assert report["references"]["code_hash"] == run["code_hash"]
        assert report["final_status"] in {s.value for s in FinalStatus}
        assert report["final_status"] != FinalStatus.EXECUTION_FAILED.value
        assert report_resp.json()["markdown"]
        comparisons = report["metric_comparisons"]
        assert comparisons, "report must compare real metrics against the paper"
        assert all(c["experiment_value"] is not None for c in comparisons)
        assert any(c.get("paper_value") is not None for c in comparisons)

        # Budget accounting recorded real LLM + experiment consumption.
        budget = wf["budget_json"]
        assert budget["llm_calls"] >= 1
        assert budget["total_tokens"] >= 150
        assert budget["experiment_seconds"] > 0

        # Every key node left a trace; no credential material leaked into traces.
        traces = alice_client.get(f"/v1/workflows/{workflow_id}/traces").json()
        nodes = {t["node_name"] for t in traces}
        for expected in (
            "ingest_paper",
            "extract_paper",
            "build_reproduction_spec",
            "validate_evidence",
            "plan_experiment",
            "generate_code",
            "validate_code",
            "execute_code",
            "evaluate_result",
            "generate_report",
            "persist_results",
        ):
            assert expected in nodes, expected
        blob = json.dumps(traces)
        for secret in ("sk-", "pdf_b64", "service_role", "SUPABASE_SERVICE_ROLE_KEY"):
            assert secret not in blob
        llm_trace = next(t for t in traces if t["node_name"] == "build_reproduction_spec")
        assert llm_trace["token_usage"]["total_tokens"] == 150
        assert llm_trace["model_name"] == "stub"

    async def test_report_ownership_isolated(self, settings, alice_client, runner, two_users):
        _, bob = two_users
        checkpointer = make_memory_checkpointer()
        upload = alice_client.post(
            "/v1/papers",
            files={"file": ("lenet.pdf", build_pdf(), "application/pdf")},
            data={"title": "LeNet-5 paper"},
        )
        ids = upload.json()
        await drive(settings, runner, checkpointer, ids["workflow_id"])
        bob_headers = {"Authorization": f"Bearer {token_for(settings, bob.email)}"}
        for path in (
            f"/v1/workflows/{ids['workflow_id']}",
            f"/v1/workflows/{ids['workflow_id']}/traces",
            f"/v1/papers/{ids['paper_id']}/workflow",
        ):
            assert alice_client.get(path, headers=bob_headers).status_code == 404, path