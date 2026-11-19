from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_api.auth import get_anon_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import papers
from test_papers import USER_ID, FakeAuth
from test_plan_edits import make_plan
from test_runs import PROJECT_ID, FakeAdmin, FakeArq, seed_tables

PAPER_ID = uuid4()
WORKFLOW_ID = uuid4()
EXPERIMENT_ID = uuid4()
PLAN_ID = uuid4()
AUTH = {"Authorization": "Bearer good-token"}


def _workflow_row(status="awaiting_spec_review", **overrides):
    row = {
        "id": str(WORKFLOW_ID),
        "thread_id": str(WORKFLOW_ID),
        "project_id": str(PROJECT_ID),
        "paper_id": str(PAPER_ID),
        "status": status,
        "current_node": "human_review",
        "experiment_id": None,
        "run_id": None,
        "spec_version_id": None,
        "plan_id": None,
        "budget_json": {},
        "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "projects": {"user_id": str(USER_ID)},
    }
    row.update(overrides)
    return row


def _seed(**extra):
    tables = seed_tables()
    tables["projects"] = [{"id": str(PROJECT_ID), "user_id": str(USER_ID), "is_default": True}]
    tables["papers"] = [
        {
            "id": str(PAPER_ID),
            "project_id": str(PROJECT_ID),
            "title": "LeNet-5",
            "pdf_path": "u/p.pdf",
            "status": "parsed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "projects": {"user_id": str(USER_ID)},
        }
    ]
    tables["workflows"] = [_workflow_row()]
    tables.update(extra)
    return FakeAdmin(tables, owners={str(PROJECT_ID): str(USER_ID)})


@pytest.fixture
def env(monkeypatch):
    def build(admin, arq=None):
        settings = ApiSettings(
            supabase_url="http://host.docker.internal:54321",
            supabase_public_url="http://localhost:54321",
            supabase_anon_key="anon-test",
            supabase_service_role_key="service-test",
            _env_file=None,
        )
        monkeypatch.setattr(papers, "make_service_client", lambda s: admin)
        app = create_app(settings)
        app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
        with TestClient(app) as client:
            client.app.state.arq = arq if arq is not None else FakeArq()
            yield client

    return build


def test_get_workflow(env):
    admin = _seed()
    for client in env(admin):
        response = client.get(f"/v1/workflows/{WORKFLOW_ID}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "awaiting_spec_review"
        assert body["paper_id"] == str(PAPER_ID)
        assert "projects" not in body


def test_get_workflow_requires_ownership(env):
    admin = _seed()
    admin.tables["workflows"][0]["projects"] = {"user_id": str(uuid4())}
    for client in env(admin):
        assert client.get(f"/v1/workflows/{WORKFLOW_ID}", headers=AUTH).status_code == 404


def test_list_workflows_filter(env):
    admin = _seed()
    for client in env(admin):
        response = client.get(f"/v1/workflows?paper_id={PAPER_ID}", headers=AUTH)
        assert response.status_code == 200
        assert len(response.json()) == 1
        empty = client.get(f"/v1/workflows?paper_id={uuid4()}", headers=AUTH)
        assert empty.json() == []


def test_paper_workflow_latest(env):
    admin = _seed()
    for client in env(admin):
        response = client.get(f"/v1/papers/{PAPER_ID}/workflow", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["id"] == str(WORKFLOW_ID)


def test_traces_scoped_to_workflow(env):
    trace = {
        "id": str(uuid4()),
        "workflow_id": str(WORKFLOW_ID),
        "node_name": "ingest_paper",
        "input_json": {"paper_id": str(PAPER_ID)},
        "output_json": {},
        "duration_ms": 12,
        "model_name": None,
        "token_usage": None,
        "error": None,
        "retry_count": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    admin = _seed(agent_traces=[trace])
    for client in env(admin):
        response = client.get(f"/v1/workflows/{WORKFLOW_ID}/traces", headers=AUTH)
        assert response.status_code == 200
        assert [t["node_name"] for t in response.json()] == ["ingest_paper"]


def _spec(status, version=1):
    return {
        "id": str(uuid4()),
        "paper_id": str(PAPER_ID),
        "version": version,
        "origin": "extraction",
        "parent_version_id": None,
        "spec_json": {},
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_resume_spec_requires_confirmed_card(env):
    admin = _seed(reproduction_specs=[_spec("draft")])
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "spec"}, headers=AUTH
        )
        assert response.status_code == 409
        assert arq.jobs == []


def test_resume_spec_enqueues_when_confirmed(env):
    admin = _seed(reproduction_specs=[_spec("draft"), _spec("confirmed", version=2)])
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "spec"}, headers=AUTH
        )
        assert response.status_code == 200
        assert arq.jobs == [("resume_workflow_task", (str(WORKFLOW_ID), "spec"))]


def _plan(approved, unconfirmed):
    plan = make_plan()
    for assumption in plan.assumptions:
        assumption.human_confirmed = assumption.field_path not in unconfirmed
    return {
        "id": str(PLAN_ID),
        "experiment_id": str(EXPERIMENT_ID),
        "version": 1,
        "config_json": plan.model_dump(mode="json", by_alias=True),
        "budget_json": {},
        "origin": "generated",
        "approved_at": "2026-01-01T00:00:00+00:00" if approved else None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _plan_seed(approved=True, unconfirmed=None):
    return _seed(
        workflows=[
            _workflow_row(
                status="awaiting_plan_approval",
                experiment_id=str(EXPERIMENT_ID),
                plan_id=str(PLAN_ID),
            )
        ],
        experiments=[{"id": str(EXPERIMENT_ID), "project_id": str(PROJECT_ID)}],
        experiment_plans=[_plan(approved, unconfirmed if unconfirmed is not None else [])],
    )


def test_resume_plan_enqueues_when_approved(env):
    admin = _plan_seed()
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "plan"}, headers=AUTH
        )
        assert response.status_code == 200
        assert arq.jobs == [("resume_workflow_task", (str(WORKFLOW_ID), "plan"))]


def test_resume_plan_requires_approval(env):
    admin = _plan_seed(approved=False)
    for client in env(admin):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "plan"}, headers=AUTH
        )
        assert response.status_code == 409


def test_resume_plan_requires_confirmed_params(env):
    admin = _plan_seed(unconfirmed=["training_config.loss_function"])
    for client in env(admin):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "plan"}, headers=AUTH
        )
        assert response.status_code == 409


def test_resume_rejects_wrong_status(env):
    admin = _seed(workflows=[_workflow_row(status="executing")])
    for client in env(admin):
        response = client.post(
            f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "spec"}, headers=AUTH
        )
        assert response.status_code == 409


def test_upload_creates_workflow(env):
    admin = _seed()
    admin.tables["workflows"] = []
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(
            "/v1/papers",
            files={"file": ("p.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"title": "LeNet-5"},
            headers=AUTH,
        )
        assert response.status_code == 202
        body = response.json()
        assert body["workflow_id"] == body["thread_id"]
        assert len(admin.tables["workflows"]) == 1
        assert admin.tables["workflows"][0]["status"] == "uploaded"
        assert arq.jobs == [("run_workflow_task", (body["workflow_id"],))]


def test_retry_failed_enqueues_recovery(env):
    admin = _seed(workflows=[_workflow_row(status="failed", error="boom")])
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH)
        assert response.status_code == 200
        assert arq.jobs == [("recover_workflow_task", (str(WORKFLOW_ID),))]
        row = admin.tables["workflows"][0]
        assert row["status"] == "parsing"
        assert row["error"] is None


def test_retry_failed_with_experiment_resumes_executing(env):
    admin = _seed(
        workflows=[
            _workflow_row(status="failed", error="boom", experiment_id=str(EXPERIMENT_ID))
        ]
    )
    for client in env(admin):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH)
        assert response.status_code == 200
        assert admin.tables["workflows"][0]["status"] == "executing"


def test_retry_rejects_non_failed(env):
    admin = _seed(workflows=[_workflow_row(status="completed")])
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH)
        assert response.status_code == 409
        assert arq.jobs == []


def test_resume_dedup_same_stage(env):
    admin = _seed(reproduction_specs=[_spec("confirmed", version=2)])
    arq = FakeArq()
    for client in env(admin, arq):
        for _ in range(2):
            response = client.post(
                f"/v1/workflows/{WORKFLOW_ID}/resume", json={"stage": "spec"}, headers=AUTH
            )
            assert response.status_code == 200
        assert arq.jobs == [("resume_workflow_task", (str(WORKFLOW_ID), "spec"))]


def test_retry_rolls_back_claim_on_enqueue_failure(env):
    class FailingArq(FakeArq):
        async def enqueue_job(self, name, *args, _job_id=None):
            raise RuntimeError("redis down")

    admin = _seed(workflows=[_workflow_row(status="failed", error="boom")])
    for client in env(admin, FailingArq()):
        response = client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH)
        assert response.status_code == 503
        row = admin.tables["workflows"][0]
        assert row["status"] == "failed"
        assert row["error"] == "boom"


def test_retry_requires_ownership(env):
    admin = _seed(workflows=[_workflow_row(status="failed")])
    admin.tables["workflows"][0]["projects"] = {"user_id": str(uuid4())}
    for client in env(admin):
        assert client.post(f"/v1/workflows/{WORKFLOW_ID}/retry", headers=AUTH).status_code == 404


def test_upload_url_ingests_pdf(env, monkeypatch):
    async def fake_download(url):
        assert url == "https://arxiv.org/abs/2401.01234"
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(papers, "_download_pdf", fake_download)
    admin = _seed()
    admin.tables["workflows"] = []
    arq = FakeArq()
    for client in env(admin, arq):
        response = client.post(
            "/v1/papers",
            data={"url": "https://arxiv.org/abs/2401.01234", "title": ""},
            headers=AUTH,
        )
        assert response.status_code == 202
        paper = admin.tables["papers"][-1]
        assert paper["source"] == "url"
        assert paper["source_url"] == "https://arxiv.org/abs/2401.01234"
        assert paper["title"] == "2401.01234"
        assert arq.jobs == [("run_workflow_task", (response.json()["workflow_id"],))]


def test_upload_requires_file_or_url(env):
    admin = _seed()
    for client in env(admin):
        response = client.post("/v1/papers", data={"title": "x"}, headers=AUTH)
        assert response.status_code == 422
