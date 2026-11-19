"""Integration: upload -> unified workflow -> grounded card -> edits -> ownership.

Requires `supabase start` (from infra/) and .env with local keys, plus the
workflow migration applied. PDF parsing and evidence grounding run for real;
the LLM extraction step is stubbed at the HTTP boundary. The 15-node LangGraph
runs in-process with a memory checkpointer and pauses at the human spec gate.
"""

import base64
import json
from uuid import uuid4

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.worker import run_workflow_task
from reproagent_api.workflow import make_memory_checkpointer
from reproagent_paperparse import ground_card, parse_pdf
from reproagent_schemas import (
    ExtractResponse,
    GroundResponse,
    LLMUsage,
    ParsedDocument,
    ParseResponse,
    ReproductionCard,
)

from supabase import ClientOptions, create_client

MOCK_CARD = {
    "method": {
        "value": "LeNet-5",
        "status": "paper_explicit",
        "evidence_quote": "We present LeNet-5, a convolutional network.",
        "confidence": 0.95,
    },
    "model_architecture": {
        "value": "convolutional layers",
        "status": "paper_explicit",
        "evidence_quote": "Layer C3 is a convolutional layer with 16 feature maps.",
        "confidence": 0.9,
    },
    "dataset": {
        "value": "MNIST",
        "status": "paper_explicit",
        "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
        "confidence": 0.9,
    },
    "hyperparameters": {
        "learning_rate": {
            "value": 0.001,
            "status": "paper_explicit",
            "evidence_quote": "The learning rate is set to 0.001 for the first two epochs.",
            "confidence": 0.95,
        }
    },
    "optimizer": {"value": "sgd", "status": "inferred", "confidence": 0.5},
    "loss_function": {"value": None, "status": "missing"},
    "reported_results": [
        {
            "value": 0.8,
            "metric": "error_rate",
            "status": "paper_explicit",
            "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
            "confidence": 0.95,
        }
    ],
    "missing_information": ["batch size"],
}


def build_pdf() -> bytes:
    doc = pymupdf.open()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 80), "Gradient-Based Learning Applied to Document Recognition", fontsize=18)
    p1.insert_text((72, 160), "ABSTRACT", fontsize=12)
    p1.insert_text((72, 180), "We present LeNet-5, a convolutional network.", fontsize=10)
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 80), "II. THE LENET-5 ARCHITECTURE", fontsize=12)
    p2.insert_text(
        (72, 110), "Layer C3 is a convolutional layer with 16 feature maps.", fontsize=10
    )
    p2.insert_text(
        (72, 130), "The learning rate is set to 0.001 for the first two epochs.", fontsize=10
    )
    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((72, 80), "III. RESULTS", fontsize=12)
    p3.insert_text((72, 110), "The error rate on the MNIST test set is 0.8 percent.", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def mock_agent_transport(fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        if path == "/v1/parse":
            document = parse_pdf(base64.b64decode(body["pdf_b64"]))
            payload = ParseResponse(document=document).model_dump(mode="json")
            return httpx.Response(200, json=payload)
        if path == "/v1/extract":
            if fail:
                return httpx.Response(502, json={"detail": "LLM unavailable"})
            usage = LLMUsage(
                model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
            )
            response = ExtractResponse(card_raw=MOCK_CARD, usages=[usage])
            return httpx.Response(200, json=response.model_dump(mode="json"))
        if path == "/v1/ground":
            doc = ParsedDocument.model_validate(body["document"])
            card = ReproductionCard.model_validate(ground_card(body["card_raw"], doc))
            return httpx.Response(200, json=GroundResponse(card=card).model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected path {path}"})

    return httpx.MockTransport(handler)


class FakeArq:
    def __init__(self):
        self.enqueued: list[tuple[str, tuple]] = []

    async def enqueue_job(self, name: str, *args):
        self.enqueued.append((name, args))

    async def aclose(self):
        pass


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
                "email": f"{name}-u-{suffix}@example.test",
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
        client.app.state.arq = FakeArq()
        client.headers["Authorization"] = f"Bearer {token_for(settings, alice.email)}"
        yield client


@pytest.fixture(scope="module")
def bob_headers(settings, two_users):
    _, bob = two_users
    return {"Authorization": f"Bearer {token_for(settings, bob.email)}"}


def upload(client, pdf: bytes) -> dict:
    response = client.post(
        "/v1/papers",
        files={"file": ("lenet.pdf", pdf, "application/pdf")},
        data={"title": "LeNet-5 paper"},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def run_worker(settings, workflow_id: str, fail: bool = False):
    ctx = {
        "settings": settings,
        "agent_transport": mock_agent_transport(fail),
        "checkpointer": make_memory_checkpointer(),
    }
    await run_workflow_task(ctx, workflow_id)


def workflow(client, workflow_id: str) -> dict:
    response = client.get(f"/v1/workflows/{workflow_id}")
    assert response.status_code == 200, response.text
    return response.json()


class TestUnderstandingFlow:
    async def test_upload_workflow_produces_grounded_card(self, settings, alice_client):
        ids = upload(alice_client, build_pdf())
        assert workflow(alice_client, ids["workflow_id"])["status"] == "uploaded"

        await run_worker(settings, ids["workflow_id"])

        wf = workflow(alice_client, ids["workflow_id"])
        assert wf["status"] == "awaiting_spec_review"
        assert wf["spec_version_id"] is not None

        card = alice_client.get(f"/v1/papers/{ids['paper_id']}/card").json()
        assert card["version"] == 1
        assert card["origin"] == "extraction"
        assert card["card"]["hyperparameters"]["learning_rate"]["source_page"] == 2
        assert card["card"]["reported_results"][0]["source_page"] == 3
        assert card["card"]["loss_function"]["status"] == "missing"

        paper = alice_client.get(f"/v1/papers/{ids['paper_id']}").json()
        assert paper["status"] == "parsed"

        pdf_url = alice_client.get(f"/v1/papers/{ids['paper_id']}/pdf").json()["url"]
        assert "token=" in pdf_url

    async def test_reparse_appends_version_without_touching_old(self, settings, alice_client):
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"])
        v1 = alice_client.get(f"/v1/papers/{ids['paper_id']}/card").json()

        reparse = alice_client.post(f"/v1/papers/{ids['paper_id']}/reparse")
        assert reparse.status_code == 202
        await run_worker(settings, reparse.json()["workflow_id"])

        versions = alice_client.get(f"/v1/papers/{ids['paper_id']}/versions").json()
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[1]["parent_version_id"] == v1["id"]
        v1_again = alice_client.get(f"/v1/papers/{ids['paper_id']}/card?version=1").json()
        assert v1_again["card"] == v1["card"]

    async def test_user_edit_appends_version_and_preserves_old(self, settings, alice_client):
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"])

        edit = alice_client.post(
            f"/v1/papers/{ids['paper_id']}/card/edits",
            json={
                "base_version": 1,
                "edits": [{"field_path": "optimizer", "value": "adam"}],
            },
        )
        assert edit.status_code == 201, edit.text
        v2 = edit.json()
        assert v2["version"] == 2
        assert v2["origin"] == "user_edit"
        assert v2["card"]["optimizer"]["value"] == "adam"
        assert v2["card"]["optimizer"]["human_confirmed"] is True

        v1 = alice_client.get(f"/v1/papers/{ids['paper_id']}/card?version=1").json()
        assert v1["card"]["optimizer"]["value"] == "sgd"
        assert v1["card"]["optimizer"]["human_confirmed"] is False

    async def test_edit_unknown_field_rejected(self, settings, alice_client):
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"])
        response = alice_client.post(
            f"/v1/papers/{ids['paper_id']}/card/edits",
            json={"base_version": 1, "edits": [{"field_path": "bogus", "value": "x"}]},
        )
        assert response.status_code == 422

    async def test_worker_failure_marks_workflow_failed(self, settings, alice_client):
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"], fail=True)
        wf = workflow(alice_client, ids["workflow_id"])
        assert wf["status"] == "failed"
        assert wf["error"]


class TestOwnershipIsolation:
    async def test_bob_cannot_see_alice_resources(self, settings, alice_client, bob_headers):
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"])
        for path in (
            f"/v1/papers/{ids['paper_id']}",
            f"/v1/papers/{ids['paper_id']}/card",
            f"/v1/papers/{ids['paper_id']}/versions",
            f"/v1/papers/{ids['paper_id']}/pdf",
            f"/v1/workflows/{ids['workflow_id']}",
            f"/v1/papers/{ids['paper_id']}/workflow",
        ):
            response = alice_client.get(path, headers=bob_headers)
            assert response.status_code == 404, path

    async def test_rls_blocks_cross_user_spec_access(self, settings, alice_client, two_users):
        alice, bob = two_users
        ids = upload(alice_client, build_pdf())
        await run_worker(settings, ids["workflow_id"])

        options = ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))
        anon = create_client(settings.supabase_url, settings.supabase_anon_key, options)
        session = anon.auth.sign_in_with_password(
            {"email": bob.email, "password": "test-password-123"}
        )
        bob_client = create_client(settings.supabase_url, session.session.access_token, options)
        assert bob_client.table("reproduction_specs").select("id").execute().data == []
        blocked = (
            bob_client.table("reproduction_specs")
            .update({"version": 99})
            .eq("paper_id", ids["paper_id"])
            .execute()
        )
        assert blocked.data == []