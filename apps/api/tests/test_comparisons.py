"""End-to-end comparison flow with a fake database, agent and runner.

The Agent side runs its real deterministic code (candidate scan, planning,
verification, report). Only the HTTP boundaries and Postgres are faked, so the
orchestration, pairing rules and persistence contract are genuinely exercised.
"""

import io
import json
import zipfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from reproagent_agent.codegen import generate_code, validate_bundle
from reproagent_agent.comparison import (
    UnsupportedComparisonError,
    build_comparison_plan,
    build_comparison_report,
    find_candidates,
    verify_pairs,
)
from reproagent_api import worker
from reproagent_api.auth import get_anon_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import comparisons as comparisons_router
from reproagent_schemas import (
    CandidateRequest,
    CodegenRequest,
    ComparisonPlanRequest,
    ComparisonReportRequest,
    ParsedDocument,
    ReproductionCard,
    RunAudit,
    RunResult,
    RunStatus,
    StaticValidateRequest,
    TableData,
    TextBlock,
    VerifyPairRequest,
)
from test_runs import FakeAdmin, FakeArq, make_card

USER_ID = uuid4()
BOB_ID = uuid4()
PAPER_ID = uuid4()
PROJECT_ID = uuid4()
SPEC_ID = uuid4()

TOKENS = {"good-token": USER_ID, "bob-token": BOB_ID}
AUTH = {"Authorization": "Bearer good-token"}
BOB_AUTH = {"Authorization": "Bearer bob-token"}


class FakeAuth:
    def get_user(self, token: str):
        if token not in TOKENS:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(TOKENS[token])))


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


def document() -> ParsedDocument:
    return ParsedDocument(
        num_pages=10,
        blocks=[
            TextBlock(
                id="p8-b1",
                text="We compare SGD and Adam on MNIST. Adam reaches 99% accuracy.",
                page=8,
            )
        ],
        tables=[
            TableData(
                id="t3",
                page=7,
                caption="Table 3: optimizer comparison, test accuracy",
                rows=[
                    ["Optimizer", "Learning rate", "Accuracy"],
                    ["SGD", "0.01", "90.5%"],
                    ["Adam", "0.001", "93.2%"],
                ],
            ),
            TableData(
                id="t4",
                page=9,
                caption="Table 4: ablation with a different budget, test accuracy",
                rows=[
                    ["Optimizer", "Learning rate", "Accuracy"],
                    ["SGD", "0.05", "88.0%"],
                    ["Adam", "0.0005", "91.0%"],
                ],
            ),
        ],
    )


def agent_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/comparisons/candidates":
            return httpx.Response(
                200,
                json=find_candidates(CandidateRequest.model_validate(body)).model_dump(
                    mode="json"
                ),
            )
        if path == "/v1/comparisons/plan":
            try:
                response = build_comparison_plan(
                    ComparisonPlanRequest.model_validate(body)
                )
            except UnsupportedComparisonError as exc:
                return httpx.Response(
                    422, json={"detail": {"message": str(exc), "missing": exc.gaps}}
                )
            return httpx.Response(200, json=response.model_dump(mode="json"))
        if path == "/v1/codegen":
            request_body = CodegenRequest.model_validate(body)
            bundle = generate_code(request_body.card, request_body.plan)
            return httpx.Response(200, json={"bundle": bundle.model_dump(mode="json")})
        if path == "/v1/codegen/validate":
            request_body = StaticValidateRequest.model_validate(body)
            return httpx.Response(
                200, json=validate_bundle(request_body.bundle).model_dump(mode="json")
            )
        if path == "/v1/comparisons/verify":
            request_body = VerifyPairRequest.model_validate(body)
            outcome = verify_pairs(
                request_body.plan, request_body.observations, request_body.requested_pairs
            )
            return httpx.Response(200, json={"outcome": outcome.model_dump(mode="json")})
        if path == "/v1/comparisons/report":
            request_body = ComparisonReportRequest.model_validate(body)
            response = build_comparison_report(request_body)
            return httpx.Response(200, json=response.model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    return httpx.MockTransport(handler)


ACCURACY = {"sgd": 0.86, "adam": 0.90}
CORRECT = {"sgd": 880, "adam": 921}


def runner_transport(fail_first: set[str] | None = None, image_digest: str = "img-v1"):
    """Answer based on the submitted config so pairing logic sees real configs."""
    calls: list[str] = []
    assigned: dict[str, RunResult] = {}
    fail_first = fail_first or set()
    failed: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/images/status":
            return httpx.Response(
                200,
                json={
                    "image": "reproagent-exec:latest",
                    "available": True,
                    "digest": image_digest,
                },
            )
        if request.method == "POST" and path == "/runs":
            payload = json.loads(request.content)
            config = yaml.safe_load(
                next(f["content"] for f in payload["code_files"] if f["path"] == "config.yaml")
            )
            optimizer = config["training"]["optimizer"]
            seed = config["seed"]
            calls.append(optimizer)
            run_id = str(uuid4())
            if optimizer in fail_first and optimizer not in failed:
                failed.add(optimizer)
                result = RunResult(
                    run_id=UUID(run_id),
                    status=RunStatus.FAILED,
                    exit_code=1,
                    stderr="RuntimeError: data loader crashed",
                    error="RuntimeError: data loader crashed",
                )
            else:
                accuracy = ACCURACY[optimizer]
                correct = CORRECT[optimizer]
                evaluated = 1024
                result = RunResult(
                    run_id=UUID(run_id),
                    status=RunStatus.SUCCEEDED,
                    exit_code=0,
                    stdout="ok",
                    execution_time=2.0,
                    metrics={
                        "schema_version": "1.2",
                        "target_metric": "accuracy",
                        "metric_value": accuracy,
                        "accuracy": accuracy,
                        "correct_count": correct,
                        "evaluated_count": evaluated,
                        "error_rate": 1 - accuracy,
                        "test_loss": 0.2,
                        "epochs_completed": config["training"]["epochs"],
                        "train_samples": config["data"]["train_subset_size"],
                        "test_samples": config["data"]["test_subset_size"],
                        "seed": seed,
                        "runtime_seconds": 2.0,
                        "reproducibility": {
                            "dataset_split_sha256": "split-fixed",
                            "init_weights_sha256": "init-fixed",
                            "sample_order_sha256": "order-fixed",
                            "template_sha256": "template-fixed",
                            "batch_size": config["training"]["batch_size"],
                            "epochs": config["training"]["epochs"],
                            "train_subset_size": config["data"]["train_subset_size"],
                            "test_subset_size": config["data"]["test_subset_size"],
                            "loss_function": config["training"]["loss_function"],
                            "optimizer": optimizer,
                            "learning_rate": config["training"]["learning_rate"],
                        },
                    },
                    audit=RunAudit(
                        image_digest=image_digest,
                        seed=seed,
                        code_hash="code-v1",
                        dataset_fingerprint="dataset-v1",
                    ),
                    artifacts=["metrics.json", "model.pt"],
                )
            assigned[run_id] = result
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        if request.method == "GET" and path.startswith("/runs/") and "/artifacts/" in path:
            return httpx.Response(200, content=b"artifact-bytes")
        if request.method == "GET" and path.startswith("/runs/") and path.endswith("/artifacts"):
            return httpx.Response(200, json=["metrics.json", "model.pt"])
        if request.method == "GET" and path.startswith("/runs/"):
            run_id = path.split("/")[2]
            return httpx.Response(200, json=assigned[run_id].model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def card_without_learning_rate() -> ReproductionCard:
    """A paper that names the optimizer but never states a learning rate."""
    card = make_card()
    card.hyperparameters.pop("learning_rate", None)
    return card


def seed_tables(with_document: bool = True) -> dict:
    card = card_without_learning_rate()
    return {
        "projects": [{"id": str(PROJECT_ID), "user_id": str(USER_ID)}],
        "papers": [
            {
                "id": str(PAPER_ID),
                "project_id": str(PROJECT_ID),
                "title": "LeNet-5",
                "pdf_path": "u/p.pdf",
                "projects": {"user_id": str(USER_ID)},
            }
        ],
        "reproduction_specs": [
            {
                "id": str(SPEC_ID),
                "paper_id": str(PAPER_ID),
                "version": 1,
                "spec_json": card.model_dump(mode="json"),
                "status": "confirmed",
            }
        ],
        "understanding_jobs": (
            [
                {
                    "id": str(uuid4()),
                    "paper_id": str(PAPER_ID),
                    "document": document().model_dump(mode="json"),
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
            if with_document
            else []
        ),
        "comparison_plans": [],
        "comparison_plan_versions": [],
        "comparisons": [],
        "comparison_runs": [],
        "comparison_reports": [],
        "experiments": [],
        "experiment_plans": [],
        "generated_files": [],
        "execution_runs": [],
        "execution_run_files": [],
        "execution_artifacts": [],
    }


@pytest.fixture(autouse=True)
def _single_driver_lock(monkeypatch):
    """DB-free stand-in for the advisory lock; real exclusion is integration-tested."""

    class _Lock:
        def __init__(self, acquired: bool = True):
            self.acquired = acquired

        def __enter__(self):
            return self.acquired

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(worker, "_drive_lock", lambda settings, key: _Lock(True))


@pytest.fixture
def env(monkeypatch):
    tables = seed_tables()
    admin = FakeAdmin(tables, {str(PROJECT_ID): str(USER_ID)})
    monkeypatch.setattr(comparisons_router, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    app.state.agent_transport = agent_transport()
    app.state.runner_transport = runner_transport()
    app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
    with TestClient(app) as client:
        app.state.arq = FakeArq()
        yield client, admin, app, settings


def payload(question="在相同条件下，Adam 是否比 SGD 准确率更高？", learning_rate=0.001):
    return {
        "question": question,
        "groups": [
            {
                "key": "a",
                "label": "SGD 组",
                "optimizer": "sgd",
                "learning_rate": 0.01,
                "references": [
                    {
                        "label": "表3 · SGD",
                        "quote": "SGD 0.01 90.5%",
                        "source_page": 7,
                        "table_id": "t3",
                        "metric": "accuracy",
                        "value": 0.905,
                    }
                ],
            },
            {
                "key": "b",
                "label": "Adam 组",
                "optimizer": "adam",
                "learning_rate": learning_rate,
                "references": [
                    {
                        "label": "表3 · Adam",
                        "quote": "Adam 0.001 93.2%",
                        "source_page": 7,
                        "table_id": "t3",
                        "metric": "accuracy",
                        "value": 0.932,
                    }
                ],
            },
        ],
    }


def create_plan(client, **overrides) -> dict:
    response = client.post(
        f"/v1/papers/{PAPER_ID}/comparison-plans",
        headers=AUTH,
        json=payload(**overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve_latest(client, plan_id: str) -> dict:
    detail = client.get(f"/v1/comparison-plans/{plan_id}", headers=AUTH).json()
    version = detail["latest_version"]
    response = client.post(
        f"/v1/comparison-plans/{plan_id}/versions/{version}/approve",
        headers=AUTH,
        json={"base_version": version},
    )
    assert response.status_code == 200, response.text
    return response.json()


def start(client, plan_id: str, idempotency_key: str | None = None) -> httpx.Response:
    detail = client.get(f"/v1/comparison-plans/{plan_id}", headers=AUTH).json()
    version = detail["latest_version"]
    headers = dict(AUTH)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        f"/v1/comparison-plans/{plan_id}/versions/{version}/start",
        headers=headers,
        json={"base_version": version},
    )


async def run_job(app, settings, comparison_id: str) -> None:
    await worker.run_comparison_task(
        {
            "settings": settings,
            "agent_transport": app.state.agent_transport,
            "runner_transport": app.state.runner_transport,
        },
        comparison_id,
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_candidates_list_two_groups_with_page_evidence(env):
    client, _, _, _ = env
    response = client.post(
        f"/v1/papers/{PAPER_ID}/comparison-candidates",
        headers=AUTH,
        json={"question": "Adam 比 SGD 更好吗？"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["support"]["ok"] is True
    candidate = body["candidates"][0]
    assert candidate["comparable"] is True
    assert {ref["source_page"] for ref in candidate["references"]} == {7}


def test_create_plan_keeps_missing_params_pending(env):
    client, _, _, _ = env
    detail = create_plan(client, learning_rate=None)
    pending = detail["plan"]["pending"]
    assert any(item["id"] == "groups.b.learning_rate.value" for item in pending)
    assert detail["approved_version"] is None

    response = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions/{detail['latest_version']}/approve",
        headers=AUTH,
        json={"base_version": detail["latest_version"]},
    )
    assert response.status_code == 409
    assert "未确认" in response.json()["detail"]


def test_adopting_suggestion_and_leaving_page_restores_state(env):
    client, _, _, _ = env
    detail = create_plan(client, learning_rate=None)
    pending_id = "groups.b.learning_rate.value"
    revised = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "采用建议学习率",
            "adopt_pending_ids": [pending_id],
        },
    )
    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert body["latest_version"] == detail["latest_version"] + 1
    resolved = next(item for item in body["plan"]["pending"] if item["id"] == pending_id)
    assert resolved["status"] == "adopted"
    assert resolved["resolved_value"] == 0.001

    reloaded = client.get(f"/v1/comparison-plans/{detail['id']}", headers=AUTH).json()
    again = next(item for item in reloaded["plan"]["pending"] if item["id"] == pending_id)
    assert again["status"] == "adopted"
    assert approve_latest(client, detail["id"])["approved_version"] == body["latest_version"]


def test_start_is_idempotent_per_key(env):
    client, admin, _, _ = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    first = start(client, detail["id"], "req-1")
    second = start(client, detail["id"], "req-1")
    assert first.status_code == second.status_code == 202
    assert first.json()["comparison_id"] == second.json()["comparison_id"]
    assert len(admin.tables["comparisons"]) == 1
    assert len(admin.tables["comparison_runs"]) == 0


@pytest.mark.asyncio
async def test_pair_runs_and_report_end_to_end(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    await run_job(app, settings, comparison_id)

    stored = admin.tables["comparisons"][0]
    assert stored["status"] == "completed"
    assert stored["completed_pairs"] == 1
    assert len(admin.tables["comparison_runs"]) == 2

    body = client.get(f"/v1/comparisons/{comparison_id}", headers=AUTH).json()
    assert body["status"] == "completed"
    assert len(body["runs"]) == 2
    outcome = body["outcome"]
    assert outcome["status"] == "completed"
    assert outcome["mean_a"] == pytest.approx(880 / 1024)
    assert outcome["mean_b"] == pytest.approx(921 / 1024)
    assert outcome["direction"] == "b_higher"
    assert outcome["mean_delta_percentage_points"] == pytest.approx((880 - 921) / 1024 * 100)

    report = client.get(f"/v1/comparisons/{comparison_id}/report", headers=AUTH)
    assert report.status_code == 200
    markdown = report.json()["markdown"]
    assert "85.94%" in markdown and "89.94%" in markdown
    assert "配对核验" in markdown

    export = client.get(f"/v1/comparisons/{comparison_id}/export", headers=AUTH)
    assert export.status_code == 200
    assert export.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        names = set(archive.namelist())
        assert "report.md" in names and "plan.json" in names
        assert any(name.startswith("runs/seed-42-a") for name in names)
        assert any(name.endswith("config.yaml") for name in names)
        assert any(name.endswith("metrics.json") or "run.json" in name for name in names)


@pytest.mark.asyncio
async def test_failed_group_is_retried_alone(env):
    client, admin, app, settings = env
    app.state.runner_transport = runner_transport(fail_first={"adam"})
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    await run_job(app, settings, comparison_id)

    attempts = {
        (row["group_key"], row["attempt"]) for row in admin.tables["comparison_runs"]
    }
    assert ("a", 1) in attempts
    assert ("b", 1) in attempts and ("b", 2) in attempts
    assert admin.tables["comparisons"][0]["status"] == "completed"
    body = client.get(f"/v1/comparisons/{comparison_id}", headers=AUTH).json()
    assert len(body["runs"]) == 3
    assert sum(1 for run in body["runs"] if run["group_key"] == "a") == 1


@pytest.mark.asyncio
async def test_same_config_failure_twice_leaves_comparison_incomplete(env):
    client, admin, app, settings = env

    def always_fail(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/images/status":
            return httpx.Response(
                200, json={"image": "i", "available": True, "digest": "img-v1"}
            )
        if request.method == "POST" and path == "/runs":
            run_id = str(uuid4())
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        if request.method == "GET" and path.startswith("/runs/"):
            run_id = path.split("/")[2]
            return httpx.Response(
                200,
                json=RunResult(
                    run_id=UUID(run_id),
                    status=RunStatus.FAILED,
                    exit_code=1,
                    error="boom",
                    stderr="boom",
                ).model_dump(mode="json"),
            )
        return httpx.Response(404, json={"detail": "nope"})

    app.state.runner_transport = httpx.MockTransport(always_fail)
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    await run_job(app, settings, comparison_id)

    # Both groups failed identically: no pair exists, so the comparison is a
    # failure, not a quiet "no difference".
    assert admin.tables["comparisons"][0]["status"] == "failed"
    report = client.get(f"/v1/comparisons/{comparison_id}/report", headers=AUTH).json()
    assert report["status"] == "incomplete"
    assert report["report_json"]["outcome"]["mean_delta"] is None
    assert "不给出任何差值结论" in report["report_json"]["summary"]


@pytest.mark.asyncio
async def test_changing_the_question_keeps_old_comparison_unfinished(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    revised = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "换一个更严格的问题",
            "question": "在相同条件下，Adam 是否比 SGD 训练损失更低？",
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["latest_version"] == detail["latest_version"] + 1
    old = client.get(f"/v1/comparisons/{comparison_id}", headers=AUTH).json()
    assert old["status"] == "incomplete"


def test_foreign_user_cannot_read_plan_or_comparison(env):
    client, admin, _, _ = env
    detail = create_plan(client)
    assert (
        client.get(f"/v1/comparison-plans/{detail['id']}", headers=BOB_AUTH).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_rerun_refuses_a_changed_image_then_allows_current_environment(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]
    await run_job(app, settings, comparison_id)

    app.state.runner_transport = runner_transport(image_digest="img-v2")
    blocked = client.post(
        f"/v1/comparisons/{comparison_id}/rerun",
        headers=AUTH,
        json={"use_current_environment": False, "reason": "结果复核"},
    )
    assert blocked.status_code == 409
    assert "镜像摘要" in blocked.json()["detail"]

    allowed = client.post(
        f"/v1/comparisons/{comparison_id}/rerun",
        headers=AUTH,
        json={"use_current_environment": True, "reason": "换用当前环境重跑"},
    )
    assert allowed.status_code == 202, allowed.text
    new_id = allowed.json()["comparison_id"]
    assert new_id != comparison_id
    created = next(row for row in admin.tables["comparisons"] if row["id"] == new_id)
    assert created["origin"] == "rerun_current_env"
    assert created["source_comparison_id"] == comparison_id


@pytest.mark.asyncio
async def test_common_config_change_reruns_both_groups(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    first_id = start(client, detail["id"]).json()["comparison_id"]
    await run_job(app, settings, first_id)

    revised = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "共同批大小调整为 32",
            "batch_size": 32,
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["latest_version"] == detail["latest_version"] + 1
    approve_latest(client, detail["id"])
    second_id = start(client, detail["id"]).json()["comparison_id"]
    assert second_id != first_id

    await run_job(app, settings, second_id)
    second_runs = [
        row for row in admin.tables["comparison_runs"] if row["comparison_id"] == second_id
    ]
    assert {row["group_key"] for row in second_runs} == {"a", "b"}
    first_runs = admin.tables["comparison_runs"][:2]
    assert all(row["comparison_id"] == first_id for row in first_runs)


# --------------------------------------------------------------------------
# Review hardening: single driver, authority, idempotent write-back
# --------------------------------------------------------------------------


def test_duplicate_start_enqueues_one_job(env):
    client, _admin, app, _settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    first = start(client, detail["id"], "same-key")
    second = start(client, detail["id"], "same-key")
    assert first.json()["comparison_id"] == second.json()["comparison_id"]
    jobs = [job for job in app.state.arq.jobs if job[0] == "run_comparison_task"]
    assert len(jobs) == 1


def test_worker_skips_when_another_driver_holds_the_lock(env, monkeypatch):
    class _Busy:
        def __enter__(self):
            return False

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(worker, "_drive_lock", lambda settings, key: _Busy())
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]
    import asyncio

    asyncio.get_event_loop_policy()
    asyncio.run(run_job(app, settings, comparison_id))
    assert admin.tables["comparison_runs"] == []
    assert admin.tables["comparisons"][0]["status"] == "queued"


def test_out_of_range_scaling_requires_confirmation_and_adoption_clamps_it(env):
    client, _admin, _app, _settings = env
    detail = create_plan(client)
    revised = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "尝试更大的训练集",
            "train_subset_size": 120000,
        },
    )
    assert revised.status_code == 200, revised.text
    body = revised.json()
    pending = next(
        item
        for item in body["plan"]["pending"]
        if item["id"] == "scaling.train_subset_size"
    )
    assert pending["suggested_value"] == 60000
    assert body["plan"]["base_plan"]["dataset_config"]["train_subset_size"] == 120000

    blocked = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions/{body['latest_version']}/approve",
        headers=AUTH,
        json={"base_version": body["latest_version"]},
    )
    assert blocked.status_code == 409

    adopted = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": body["latest_version"],
            "change_reason": "采用授权上限",
            "adopt_pending_ids": ["scaling.train_subset_size"],
        },
    )
    assert adopted.status_code == 200, adopted.text
    plan = adopted.json()["plan"]
    assert plan["base_plan"]["dataset_config"]["train_subset_size"] == 60000
    assert not [item for item in plan["pending"] if item["status"] == "pending"]
    approved_version = approve_latest(client, detail["id"])["approved_version"]
    assert approved_version == adopted.json()["latest_version"]


def test_start_refuses_a_version_stored_out_of_authority(env):
    client, admin, _app, _settings = env
    detail = create_plan(client)
    version = detail["latest_version"]
    approve_latest(client, detail["id"])
    stored = admin.tables["comparison_plan_versions"][-1]
    payload = json.loads(json.dumps(stored["plan_json"]))
    payload["base_plan"]["dataset_config"]["train_subset_size"] = 120000
    stored["plan_json"] = payload
    response = start(client, detail["id"])
    assert response.status_code == 409
    assert "超出授权范围" in response.json()["detail"]
    assert version == detail["latest_version"]


def test_user_supplied_references_are_not_duplicated_across_versions(env):
    client, _admin, _app, _settings = env
    body = payload(learning_rate=None)
    body["groups"][1]["references"] = []
    detail = client.post(
        f"/v1/papers/{PAPER_ID}/comparison-plans", headers=AUTH, json=body
    ).json()
    assert any(item["id"] == "groups.b.references" for item in detail["plan"]["pending"])

    v2 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "补充原文出处",
            "provided_pending": {"groups.b.references": "附录 B 表 5"},
        },
    ).json()
    v3 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": v2["latest_version"],
            "change_reason": "采用建议学习率",
            "adopt_pending_ids": ["groups.b.learning_rate.value"],
        },
    ).json()
    user_refs = [
        ref
        for ref in v3["plan"]["groups"][1]["references"]
        if ref["label"] == "附录 B 表 5"
    ]
    assert len(user_refs) == 1
    assert len(v3["plan"]["groups"][1]["references"]) == 1


def test_invalid_user_value_returns_422_not_500(env):
    client, _admin, _app, _settings = env
    detail = create_plan(client, learning_rate=None)
    response = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "手填学习率",
            "provided_pending": {"groups.b.learning_rate.value": "0.001（表3）"},
        },
    )
    assert response.status_code == 422
    assert "需要一个数值" in response.json()["detail"]


def test_question_change_keeps_batch_size_and_rejects_mixed_edits(env):
    client, _admin, _app, _settings = env
    detail = create_plan(client)
    v2 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "批大小改为 32",
            "batch_size": 32,
        },
    ).json()
    assert v2["plan"]["base_plan"]["training_config"]["batch_size"]["value"] == 32

    mixed = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": v2["latest_version"],
            "change_reason": "改题同时改参数",
            "question": "Adam 与 SGD 的测试损失是否有差异？",
            "batch_size": 16,
        },
    )
    assert mixed.status_code == 400

    v3 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": v2["latest_version"],
            "change_reason": "改题",
            "question": "Adam 与 SGD 的测试损失是否有差异？",
        },
    )
    assert v3.status_code == 200, v3.text
    assert v3.json()["plan"]["base_plan"]["training_config"]["batch_size"]["value"] == 32


@pytest.mark.asyncio
async def test_attempts_are_capped_and_each_attempt_is_recorded_before_training(env):
    client, admin, app, settings = env

    def always_fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/images/status":
            return httpx.Response(
                200, json={"image": "i", "available": True, "digest": "img-v1"}
            )
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(202, json={"run_id": str(uuid4()), "status": "queued"})
        if request.method == "GET" and request.url.path.startswith("/runs/"):
            run_id = request.url.path.split("/")[2]
            return httpx.Response(
                200,
                json=RunResult(
                    run_id=UUID(run_id), status=RunStatus.FAILED, exit_code=1, error="boom"
                ).model_dump(mode="json"),
            )
        return httpx.Response(404, json={"detail": "nope"})

    app.state.runner_transport = httpx.MockTransport(always_fail)
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]
    await run_job(app, settings, comparison_id)

    attempts = sorted(
        row["attempt"]
        for row in admin.tables["comparison_runs"]
        if row["group_key"] == "a"
    )
    assert attempts == [1, 2]
    assert all(
        run["status"] in ("failed", "succeeded") for run in admin.tables["execution_runs"]
    )


@pytest.mark.asyncio
async def test_recovery_reattaches_to_an_inflight_run_instead_of_retraining(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    # Simulate a driver that died right after starting group A's container: the
    # run row is still "running" and no result has been persisted yet.
    experiment_id = str(uuid4())
    plan_row_id = str(uuid4())
    run_id = str(uuid4())
    runner_run_id = str(uuid4())
    admin.tables["experiments"].append(
        {
            "id": experiment_id,
            "project_id": str(PROJECT_ID),
            "paper_id": str(PAPER_ID),
            "spec_id": str(SPEC_ID),
            "status": "running",
            "name": "对照在飞",
        }
    )
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": experiment_id,
            "plan_id": plan_row_id,
            "status": "running",
            "runner_run_id": runner_run_id,
            "code_version": 1,
            "plan_version": 1,
            "metrics_json": {},
            "audit_json": {},
            "code_hash": "code-v1",
            "repair_attempt": 0,
        }
    )
    admin.tables["comparison_runs"].append(
        {
            "id": str(uuid4()),
            "comparison_id": comparison_id,
            "seed": 42,
            "group_key": "a",
            "run_id": run_id,
            "attempt": 1,
        }
    )

    base = runner_transport()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/runs/{runner_run_id}":
            seen.append(runner_run_id)
            recovered = RunResult(
                run_id=UUID(runner_run_id),
                status=RunStatus.SUCCEEDED,
                exit_code=0,
                stdout="ok",
                execution_time=2.0,
                metrics={
                    "schema_version": "1.2",
                    "target_metric": "accuracy",
                    "metric_value": 0.86,
                    "accuracy": 0.86,
                    "correct_count": 8600,
                    "evaluated_count": 10000,
                    "test_loss": 0.2,
                    "epochs_completed": 3,
                    "train_samples": 10000,
                    "test_samples": 10000,
                    "seed": 42,
                    "runtime_seconds": 2.0,
                    "reproducibility": {
                        "dataset_split_sha256": "split-fixed",
                        "init_weights_sha256": "init-fixed",
                        "sample_order_sha256": "order-fixed",
                        "template_sha256": "template-fixed",
                        "batch_size": 64,
                        "epochs": 3,
                        "train_subset_size": 10000,
                        "test_subset_size": 10000,
                        "loss_function": "cross_entropy",
                        "optimizer": "sgd",
                        "learning_rate": 0.01,
                    },
                },
                audit=RunAudit(image_digest="img-v1", seed=42, code_hash="code-v1"),
                artifacts=[],
            )
            return httpx.Response(200, json=recovered.model_dump(mode="json"))
        return base.handler(request)

    app.state.runner_transport = httpx.MockTransport(handler)
    await run_job(app, settings, comparison_id)

    assert seen, "the driver must poll the existing runner run"
    resumed = next(row for row in admin.tables["experiments"] if row["id"] == experiment_id)
    assert resumed["status"] == "succeeded"
    attempts_a = [
        row["attempt"] for row in admin.tables["comparison_runs"] if row["group_key"] == "a"
    ]
    assert attempts_a == [1]


@pytest.mark.asyncio
async def test_superseded_comparison_keeps_unfinished_status(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    base = runner_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/runs":
            # The user changes the question while the first group is training.
            admin.table("comparisons").update(
                {"status": "incomplete", "error": "问题已变更：原问题保留为未完成"}
            ).eq("id", comparison_id).execute()
        return base.handler(request)

    app.state.runner_transport = httpx.MockTransport(handler)
    await run_job(app, settings, comparison_id)

    row = next(item for item in admin.tables["comparisons"] if item["id"] == comparison_id)
    assert row["status"] == "incomplete"
    assert "问题已变更" in row["error"]
    report = client.get(f"/v1/comparisons/{comparison_id}/report", headers=AUTH).json()
    assert report["status"] == "incomplete"
    assert "问题已变更" in report["markdown"]


@pytest.mark.asyncio
async def test_rerun_reports_missing_environment_in_response(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]
    await run_job(app, settings, comparison_id)

    app.state.runner_transport = runner_transport(image_digest="img-v2")
    allowed = client.post(
        f"/v1/comparisons/{comparison_id}/rerun",
        headers=AUTH,
        json={"use_current_environment": True, "reason": "换环境重跑"},
    )
    assert allowed.status_code == 202
    assert any("镜像摘要" in warning for warning in allowed.json()["warnings"])


def test_repeated_out_of_range_scaling_does_not_duplicate_pending(env):
    client, _admin, _app, _settings = env
    detail = create_plan(client)
    v2 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "训练集 120k",
            "train_subset_size": 120000,
        },
    ).json()
    v3 = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": v2["latest_version"],
            "change_reason": "训练集 90k",
            "train_subset_size": 90000,
        },
    ).json()
    items = [
        item for item in v3["plan"]["pending"] if item["id"] == "scaling.train_subset_size"
    ]
    assert len(items) == 1
    assert items[0]["suggested_value"] == 60000
    assert v3["plan"]["base_plan"]["dataset_config"]["train_subset_size"] == 90000


@pytest.mark.asyncio
async def test_recovery_without_runner_id_finalizes_the_run_and_retries(env):
    client, admin, app, settings = env
    detail = create_plan(client)
    approve_latest(client, detail["id"])
    comparison_id = start(client, detail["id"]).json()["comparison_id"]

    # Crash between "status=running" and writing runner_run_id: nothing to poll.
    experiment_id = str(uuid4())
    run_id = str(uuid4())
    admin.tables["experiments"].append(
        {
            "id": experiment_id,
            "project_id": str(PROJECT_ID),
            "paper_id": str(PAPER_ID),
            "spec_id": str(SPEC_ID),
            "status": "running",
            "name": "对照半启动",
        }
    )
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": experiment_id,
            "plan_id": str(uuid4()),
            "status": "running",
            "runner_run_id": None,
            "code_version": 1,
            "plan_version": 1,
            "metrics_json": {},
            "audit_json": {},
            "repair_attempt": 0,
        }
    )
    admin.tables["comparison_runs"].append(
        {
            "id": str(uuid4()),
            "comparison_id": comparison_id,
            "seed": 42,
            "group_key": "a",
            "run_id": run_id,
            "attempt": 1,
        }
    )

    await run_job(app, settings, comparison_id)

    orphan_run = next(row for row in admin.tables["execution_runs"] if row["id"] == run_id)
    orphan_experiment = next(
        row for row in admin.tables["experiments"] if row["id"] == experiment_id
    )
    assert orphan_run["status"] == "failed"
    assert "Runner 运行标识" in orphan_run["error"]
    assert orphan_experiment["status"] == "failed"
    attempts_a = sorted(
        row["attempt"] for row in admin.tables["comparison_runs"] if row["group_key"] == "a"
    )
    assert attempts_a == [1, 2]
    assert admin.tables["comparisons"][0]["status"] == "completed"


def test_provided_pending_value_length_is_capped(env):
    client, _admin, _app, _settings = env
    detail = create_plan(client, learning_rate=None)
    response = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "超长值",
            "provided_pending": {"groups.b.learning_rate.value": "0.001" * 200},
        },
    )
    assert response.status_code == 422


def test_question_change_with_missing_card_version_conflicts(env):
    client, admin, _app, _settings = env
    detail = create_plan(client)
    admin.tables["reproduction_specs"] = []
    response = client.post(
        f"/v1/comparison-plans/{detail['id']}/versions",
        headers=AUTH,
        json={
            "base_version": detail["latest_version"],
            "change_reason": "改题",
            "question": "Adam 与 SGD 的测试损失是否有差异？",
        },
    )
    assert response.status_code == 409
    assert "Card 版本不存在" in response.json()["detail"]
