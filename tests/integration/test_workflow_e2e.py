"""Integration: the FULL unified-workflow closed loop, driven through the HTTP API.

Upload LeNet-5 PDF -> real parse -> stub-LLM card -> real evidence grounding ->
user confirms missing info -> resume(spec) -> real deterministic plan -> user
confirms assumptions + scales to a tiny subset -> resume(plan) -> real codegen ->
real static validation -> REAL MNIST training in a locked-down Docker container ->
real metrics -> real evaluation report -> persisted, traceable results.

The LLM extraction step is stubbed at the HTTP boundary; every other agent step
runs the real deterministic engine in-process, and execution spawns a REAL
sibling container via the Runner app (ASGI transport). This is fixture-backed
workflow E2E evidence, not a real Qwen closed-loop acceptance test. State is
checkpointed to a shared memory saver so the two human gates resume the same graph thread.

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
import psycopg
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
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
    """Real Runner app + Docker client; fail when Docker/image is unavailable."""
    runner_settings = RunnerSettings(work_dir=WORK_DIR, enforce_disk_quota=False, _env_file=None)
    try:
        client = make_client(runner_settings)
        client.ping()
        client.images.get(runner_settings.exec_image)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Docker daemon or exec image unavailable: {exc}")
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def completed_workflow(settings, alice_client, runner):
    """Drive exactly one scaled LeNet-5/MNIST experiment for all E2E assertions."""
    checkpointer = make_memory_checkpointer()

    upload = alice_client.post(
        "/v1/papers",
        files={"file": ("lenet.pdf", build_pdf(), "application/pdf")},
        data={"title": "LeNet-5 paper"},
    )
    assert upload.status_code == 202, upload.text
    ids = upload.json()
    paper_id, workflow_id = ids["paper_id"], ids["workflow_id"]
    assert get_workflow(alice_client, workflow_id)["status"] == "uploaded"

    await drive(settings, runner, checkpointer, workflow_id)
    wf = get_workflow(alice_client, workflow_id)
    assert wf["status"] == "awaiting_spec_review", wf
    assert wf["spec_version_id"] is not None
    card = alice_client.get(f"/v1/papers/{paper_id}/card").json()
    assert card["card"]["loss_function"]["status"] == "missing"
    assert card["card"]["hyperparameters"]["learning_rate"]["source_page"] == 2

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

    await drive(settings, runner, checkpointer, workflow_id, stage="spec")
    wf = get_workflow(alice_client, workflow_id)
    assert wf["status"] == "awaiting_plan_approval", wf
    experiment_id = wf["experiment_id"]
    assert experiment_id is not None

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

    # Real codegen/validation, real sibling-container MNIST training, metrics,
    # evaluation and report persistence. Only LLM extraction is fixture-backed.
    await drive(settings, runner, checkpointer, workflow_id, stage="plan")
    wf = get_workflow(alice_client, workflow_id)
    assert wf["status"] == "completed", wf
    assert wf["error"] is None
    run_id = wf["run_id"]
    assert run_id is not None
    return {
        "paper_id": paper_id,
        "workflow_id": workflow_id,
        "experiment_id": experiment_id,
        "run_id": run_id,
        "plan_id": wf["plan_id"],
    }


class TestFullClosedLoop:
    def test_service_role_has_required_storage_privileges(self, settings):
        """Prove required platform Storage ACLs, not an exact application-owned matrix."""
        with psycopg.connect(
            settings.supabase_db_url, connect_timeout=5, row_factory=dict_row
        ) as connection:
            privileges = connection.execute(
                "select "
                "has_table_privilege('service_role', 'storage.objects', 'SELECT') "
                "as object_select, "
                "has_table_privilege('service_role', 'storage.objects', 'INSERT') "
                "as object_insert, "
                "has_table_privilege('service_role', 'storage.objects', 'UPDATE') "
                "as object_update, "
                "has_table_privilege('service_role', 'storage.objects', 'DELETE') "
                "as object_delete, "
                "has_table_privilege('service_role', 'storage.buckets', 'SELECT') "
                "as bucket_select, "
                "has_table_privilege('service_role', 'storage.buckets', 'INSERT') "
                "as bucket_insert"
            ).fetchone()
        # Supabase may grant additional platform-managed privileges. The closed
        # loop below exercises the actual service-role upload/upsert/download path.
        assert privileges == {
            "object_select": True,
            "object_insert": True,
            "object_update": True,
            "object_delete": True,
            "bucket_select": True,
            "bucket_insert": True,
        }

    async def test_upload_to_report_with_real_training(self, alice_client, completed_workflow):
        workflow_id = completed_workflow["workflow_id"]
        experiment_id = completed_workflow["experiment_id"]
        run_id = completed_workflow["run_id"]
        wf = get_workflow(alice_client, workflow_id)
        assert experiment_id == wf["experiment_id"]

        # The run really executed and produced genuine metrics + audit.
        run = alice_client.get(f"/v1/runs/{run_id}").json()
        assert run["status"] == RunStatus.SUCCEEDED.value, run
        target_metric = run["metrics_json"]["target_metric"]
        assert target_metric in ("accuracy", "error_rate")
        metrics = run["metrics_json"]
        assert metrics["metric_value"] == metrics[target_metric]
        assert metrics["accuracy"] == pytest.approx(
            metrics["correct_count"] / metrics["evaluated_count"]
        )
        assert metrics["error_rate"] == pytest.approx(1 - metrics["accuracy"])
        examples = alice_client.get(f"/v1/runs/{run_id}/prediction-examples")
        assert examples.status_code == 200, examples.text
        assert [s["index"] for s in examples.json()["samples"]] == list(range(12))
        assert run["audit_json"]["seed"] == 42
        assert run["audit_json"]["dataset_fingerprint"]
        artifacts_resp = alice_client.get(f"/v1/runs/{run_id}/artifacts")
        assert artifacts_resp.status_code == 200, artifacts_resp.text
        artifacts = artifacts_resp.json()
        assert artifacts and all(a["storage_path"].startswith(f"{run_id}/") for a in artifacts)

        # 10. The report is linked to the real execution record.
        report_resp = alice_client.get(f"/v1/runs/{run_id}/report")
        assert report_resp.status_code == 200, report_resp.text
        report = report_resp.json()["report_json"]
        assert report_resp.json()["execution_attempt"] == 1
        assert report["references"]["run_id"] == run_id
        assert report["references"]["execution_attempt"] == 1
        assert report["references"]["plan_id"] == completed_workflow["plan_id"]
        assert report["references"]["plan_version"] == 2
        assert report["references"]["code_hash"] == run["code_hash"]
        assert report["final_status"] in {s.value for s in FinalStatus}
        assert report["final_status"] != FinalStatus.EXECUTION_FAILED.value
        assert report_resp.json()["markdown"]
        rows = {row["key"]: row for row in report["parameter_comparisons"]}
        assert rows["training_samples"]["actual"] == "256"
        assert rows["training_samples"]["actual_evidence"]["kind"] == "metrics"
        assert rows["batch_size"]["actual_evidence"]["kind"] == "config"
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

    async def test_two_account_ownership_and_guessed_ids_are_isolated(
        self, settings, alice_client, two_users, completed_workflow
    ):
        _, bob = two_users
        paper_id = completed_workflow["paper_id"]
        workflow_id = completed_workflow["workflow_id"]
        experiment_id = completed_workflow["experiment_id"]
        run_id = completed_workflow["run_id"]
        artifact_ids = {
            row["id"] for row in alice_client.get(f"/v1/runs/{run_id}/artifacts").json()
        }
        guessed = uuid4()

        with TestClient(create_app(settings)) as bob_client:
            bob_client.headers["Authorization"] = f"Bearer {token_for(settings, bob.email)}"
            listed = bob_client.get("/v1/papers")
            assert listed.status_code == 200, listed.text
            assert all(row["id"] != paper_id for row in listed.json())

            foreign_get = [
                f"/v1/papers/{paper_id}",
                f"/v1/papers/{paper_id}/card",
                f"/v1/papers/{paper_id}/experiments",
                f"/v1/papers/{paper_id}/pdf",
                f"/v1/papers/{paper_id}/workflow",
                f"/v1/experiments/{experiment_id}",
                f"/v1/experiments/{experiment_id}/plans",
                f"/v1/experiments/{experiment_id}/plans/1",
                f"/v1/experiments/{experiment_id}/plans/2",
                f"/v1/experiments/{experiment_id}/runs",
                f"/v1/runs/{run_id}",
                f"/v1/runs/{run_id}/logs",
                f"/v1/runs/{run_id}/artifacts",
                f"/v1/runs/{run_id}/files",
                f"/v1/runs/{run_id}/report",
                f"/v1/runs/{run_id}/prediction-examples",
                f"/v1/workflows/{workflow_id}",
                f"/v1/workflows/{workflow_id}/traces",
                f"/v1/runs/{guessed}/logs",
                f"/v1/runs/{guessed}/artifacts",
                f"/v1/runs/{guessed}/report",
                f"/v1/workflows/{guessed}/traces",
            ]
            for path in foreign_get:
                response = bob_client.get(path)
                assert response.status_code in {403, 404}, (
                    path, response.status_code, response.text,
                )

            foreign_post = [
                (f"/v1/papers/{paper_id}/experiments", {"name": "stolen"}, {}),
                (f"/v1/papers/{paper_id}/card/edits", {
                    "base_version": 1,
                    "edits": [{"field_path": "loss_function", "value": "stolen"}],
                }, {}),
                (f"/v1/experiments/{experiment_id}/plans", {
                    "base_version": 1,
                    "review_mode": "assisted",
                    "change_reason": "stolen",
                    "changes": [],
                }, {}),
                (f"/v1/experiments/{experiment_id}/plans/1/approve", None, {}),
                (f"/v1/experiments/{experiment_id}/runs", None,
                 {"headers": {"Idempotency-Key": "foreign-e2e-run"}}),
                (f"/v1/runs/{run_id}/report", None, {}),
                (f"/v1/runs/{run_id}/cancel", None, {}),
                (f"/v1/workflows/{workflow_id}/cancel", None, {}),
                (f"/v1/workflows/{workflow_id}/resume", {"stage": "spec"}, {}),
                (f"/v1/workflows/{workflow_id}/retry", None, {}),
            ]
            for path, body, kwargs in foreign_post:
                response = bob_client.post(path, json=body, **kwargs)
                assert response.status_code in {403, 404}, (
                    path, response.status_code, response.text,
                )

        # Foreign denials and mutations did not alter the owner's terminal truth.
        assert get_workflow(alice_client, workflow_id)["status"] == "completed"
        assert alice_client.get(f"/v1/runs/{run_id}").json()["status"] == RunStatus.SUCCEEDED.value
        assert {
            row["id"] for row in alice_client.get(f"/v1/runs/{run_id}/artifacts").json()
        } == artifact_ids
        owner_report = alice_client.get(f"/v1/runs/{run_id}/report").json()
        assert owner_report["version"] == 1
        assert owner_report["execution_attempt"] == 1