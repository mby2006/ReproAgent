"""Integration: card -> plan v1 -> edit/OOM versioning -> approval gate -> RLS.

Requires `supabase start` (from infra/) with the planner migration applied, and
.env with local keys. The agent is mocked at the HTTP boundary but runs the real
deterministic planning engine, so plan content is genuine.
"""

import json
from uuid import uuid4

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from reproagent_agent.planning import build_plan
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_schemas import PlanResponse, ReproductionCard

from supabase import ClientOptions, create_client

CARD = {
    "method": {
        "value": "LeNet-5 gradient-based learning",
        "status": "paper_explicit",
        "evidence_quote": "We present LeNet-5, a convolutional network.",
        "source_page": 1,
    },
    "model_architecture": {
        "value": "LeNet-5 convolutional network",
        "status": "paper_explicit",
        "evidence_quote": "Layer C3 is a convolutional layer with 16 feature maps.",
        "source_page": 2,
    },
    "dataset": {
        "value": "MNIST",
        "status": "paper_explicit",
        "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
        "source_page": 3,
    },
    "optimizer": {
        "value": "stochastic diagonal Levenberg-Marquardt",
        "status": "paper_explicit",
        "evidence_quote": "We use a stochastic diagonal Levenberg-Marquardt method.",
        "source_page": 4,
    },
    "loss_function": {"value": None, "status": "missing"},
    "hyperparameters": {
        "learning_rate": {
            "value": 0.001,
            "status": "paper_explicit",
            "evidence_quote": "The learning rate is set to 0.001.",
            "source_page": 4,
        },
        "batch_size": {
            "value": 128,
            "status": "paper_explicit",
            "evidence_quote": "batch size 128",
            "source_page": 4,
        },
        "epochs": {
            "value": 20,
            "status": "paper_explicit",
            "evidence_quote": "20 epochs",
            "source_page": 4,
        },
    },
    "evaluation_metrics": [
        {
            "value": "accuracy",
            "status": "paper_explicit",
            "evidence_quote": "error rate on the MNIST test set",
            "source_page": 3,
        }
    ],
}


def build_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 80), "Gradient-Based Learning Applied to Document Recognition", fontsize=16
    )
    data = doc.tobytes()
    doc.close()
    return data


def mock_plan_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        card = ReproductionCard.model_validate(body["card"])
        plan = build_plan(card)
        return httpx.Response(200, json=PlanResponse(plan=plan).model_dump(mode="json"))

    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def settings() -> ApiSettings:
    return ApiSettings()


@pytest.fixture(scope="module")
def admin(settings):
    return make_service_client(settings)


@pytest.fixture(scope="module")
def two_users(admin):
    suffix = uuid4().hex[:12]
    users = []
    for name in ("alice", "bob"):
        created = admin.auth.admin.create_user(
            {
                "email": f"{name}-p-{suffix}@example.test",
                "password": "test-password-123",
                "email_confirm": True,
            }
        )
        users.append(created.user)
    yield users
    for user in users:
        admin.auth.admin.delete_user(user.id)


def token_for(settings, email: str) -> str:
    options = ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))
    anon = create_client(settings.supabase_url, settings.supabase_anon_key, options)
    session = anon.auth.sign_in_with_password({"email": email, "password": "test-password-123"})
    return session.session.access_token


@pytest.fixture(scope="module")
def alice_client(settings, two_users):
    alice, _ = two_users
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.agent_transport = mock_plan_transport()
        client.headers["Authorization"] = f"Bearer {token_for(settings, alice.email)}"
        yield client


@pytest.fixture(scope="module")
def bob_headers(settings, two_users):
    _, bob = two_users
    return {"Authorization": f"Bearer {token_for(settings, bob.email)}"}


def seed_experiment(alice_client, admin) -> str:
    upload = alice_client.post(
        "/v1/papers",
        files={"file": ("lenet.pdf", build_pdf(), "application/pdf")},
        data={"title": "LeNet-5 paper"},
    )
    assert upload.status_code == 202, upload.text
    paper_id = upload.json()["paper_id"]
    admin.table("reproduction_specs").insert(
        {
            "paper_id": paper_id,
            "version": 1,
            "origin": "extraction",
            "spec_json": CARD,
        }
    ).execute()
    created = alice_client.post(f"/v1/papers/{paper_id}/experiments", json={})
    assert created.status_code == 201, created.text
    return created.json()["id"]


class TestPlannerFlow:
    def test_generate_plan_v1(self, alice_client, admin):
        experiment_id = seed_experiment(alice_client, admin)
        detail = alice_client.get(f"/v1/experiments/{experiment_id}").json()
        assert detail["status"] == "planning"
        plan = detail["plan"]
        assert plan["version"] == 1
        assert plan["origin"] == "generated"
        content = plan["plan"]
        assert content["model_config"]["architecture"] == "lenet-5"
        assert content["training_config"]["batch_size"]["source"] == "mvp_scaling"
        assert content["training_config"]["batch_size"]["value"] == 64
        assert content["training_config"]["learning_rate"]["source"] == "paper_explicit"
        paths = {d["field_path"] for d in content["deviations_from_paper"]}
        assert "training_config.batch_size" in paths
        unconfirmed = {
            a["field_path"] for a in content["assumptions"] if a["requires_confirmation"]
        }
        assert "training_config.optimizer" in unconfirmed
        assert "training_config.loss_function" in unconfirmed

    def test_edit_appends_version_and_preserves_old(self, alice_client, admin):
        experiment_id = seed_experiment(alice_client, admin)
        v1 = alice_client.get(f"/v1/experiments/{experiment_id}/plans/1").json()
        edit = alice_client.post(
            f"/v1/experiments/{experiment_id}/plans",
            json={
                "base_version": 1,
                "changes": [{"field_path": "training_config.batch_size", "value": 32}],
                "change_reason": "OOM：降低批大小",
                "origin": "oom_retry",
                "related_error": "RuntimeError: out of memory",
            },
        )
        assert edit.status_code == 201, edit.text
        v2 = edit.json()
        assert v2["version"] == 2
        assert v2["origin"] == "oom_retry"
        assert v2["parent_version_id"] == v1["id"]
        assert v2["related_error"] == "RuntimeError: out of memory"
        assert v2["changes"][0]["before"] == 64
        assert v2["changes"][0]["after"] == 32
        assert v2["plan"]["training_config"]["batch_size"]["value"] == 32
        v1_again = alice_client.get(f"/v1/experiments/{experiment_id}/plans/1").json()
        assert v1_again["plan"]["training_config"]["batch_size"]["value"] == 64
        versions = alice_client.get(f"/v1/experiments/{experiment_id}/plans").json()
        assert [v["version"] for v in versions] == [1, 2]

    def test_approve_gate_blocks_then_passes(self, alice_client, admin):
        experiment_id = seed_experiment(alice_client, admin)
        blocked = alice_client.post(f"/v1/experiments/{experiment_id}/plans/1/approve")
        assert blocked.status_code == 409
        confirm = alice_client.post(
            f"/v1/experiments/{experiment_id}/plans",
            json={
                "base_version": 1,
                "changes": [
                    {"field_path": "training_config.optimizer", "value": "sgd"},
                    {"field_path": "training_config.loss_function", "value": "cross_entropy"},
                ],
                "change_reason": "确认关键假设",
            },
        )
        assert confirm.status_code == 201, confirm.text
        approved = alice_client.post(f"/v1/experiments/{experiment_id}/plans/2/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_at"] is not None
        detail = alice_client.get(f"/v1/experiments/{experiment_id}").json()
        assert detail["status"] == "approved"

    def test_config_immutable_trigger(self, alice_client, admin):
        experiment_id = seed_experiment(alice_client, admin)
        row = (
            admin.table("experiment_plans")
            .select("id,config_json")
            .eq("experiment_id", experiment_id)
            .eq("version", 1)
            .single()
            .execute()
            .data
        )
        with pytest.raises(Exception, match="immutable"):
            admin.table("experiment_plans").update({"config_json": {}}).eq(
                "id", row["id"]
            ).execute()
        # approved_at is the only mutable column
        admin.table("experiment_plans").update({"approved_at": "2026-01-01T00:00:00+00:00"}).eq(
            "id", row["id"]
        ).execute()


class TestOwnershipIsolation:
    def test_bob_cannot_see_alice_experiment(self, alice_client, admin, bob_headers):
        experiment_id = seed_experiment(alice_client, admin)
        for path in (
            f"/v1/experiments/{experiment_id}",
            f"/v1/experiments/{experiment_id}/plans",
            f"/v1/experiments/{experiment_id}/plans/1",
        ):
            response = alice_client.get(path, headers=bob_headers)
            assert response.status_code == 404, path
        blocked = alice_client.post(
            f"/v1/experiments/{experiment_id}/plans/1/approve", headers=bob_headers
        )
        assert blocked.status_code == 404

    def test_rls_blocks_cross_user_plan_access(self, settings, alice_client, admin, two_users):
        _, bob = two_users
        seed_experiment(alice_client, admin)
        options = ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))
        anon = create_client(settings.supabase_url, settings.supabase_anon_key, options)
        session = anon.auth.sign_in_with_password(
            {"email": bob.email, "password": "test-password-123"}
        )
        bob_client = create_client(settings.supabase_url, session.session.access_token, options)
        assert bob_client.table("experiment_plans").select("id").execute().data == []
        assert bob_client.table("experiments").select("id").execute().data == []
