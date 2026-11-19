import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from reproagent_agent.planning import build_plan
from reproagent_api.auth import get_anon_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import experiments
from reproagent_schemas import (
    EvidenceField,
    EvidenceStatus,
    PlanResponse,
    ReproductionCard,
)

USER_ID = uuid4()
BOB_ID = uuid4()
PROJECT_ID = uuid4()
PAPER_ID = uuid4()
SPEC_ID = uuid4()

TOKENS = {"good-token": USER_ID, "bob-token": BOB_ID}
AUTH = {"Authorization": "Bearer good-token"}
BOB_AUTH = {"Authorization": "Bearer bob-token"}


class FakeAuth:
    def get_user(self, token: str):
        if token not in TOKENS:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(TOKENS[token])))


def make_card() -> ReproductionCard:
    def explicit(value, page=8):
        return EvidenceField(
            value=value,
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote=f"...{value}...",
            source_page=page,
        )

    return ReproductionCard(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("stochastic diagonal Levenberg-Marquardt"),
        loss_function=EvidenceField(status=EvidenceStatus.MISSING),
        hyperparameters={
            "learning_rate": explicit("0.001"),
            "batch_size": EvidenceField(
                value=128,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="batch 128",
                source_page=9,
            ),
            "epochs": EvidenceField(
                value=20,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="20 epochs",
                source_page=9,
            ),
        },
        evaluation_metrics=[explicit("accuracy")],
    )


PLAN_DEFAULTS = {
    "approved_at": None,
    "parent_version_id": None,
    "change_reason": None,
    "related_error": None,
    "related_run_id": None,
    "changes_json": [],
    "origin": "generated",
    "budget_json": {},
}
EXPERIMENT_DEFAULTS = {"spec_id": None, "status": "pending"}


def _path(row, key):
    cur = row
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, admin, name):
        self._admin = admin
        self._name = name
        self._rows = admin.tables.setdefault(name, [])
        self._filters = []
        self._order = None
        self._limit = None
        self._insert = None
        self._update = None
        self._select = None

    def select(self, *args):
        self._select = ",".join(args)
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, row):
        self._insert = row
        return self

    def update(self, patch):
        self._update = patch
        return self

    def single(self):
        return self

    def execute(self):
        if self._insert is not None:
            defaults = PLAN_DEFAULTS if self._name == "experiment_plans" else {}
            if self._name == "experiments":
                defaults = EXPERIMENT_DEFAULTS
            row = {"id": str(uuid4()), "created_at": "2026-01-01T00:00:00+00:00", **defaults}
            row.update(self._insert)
            if self._name in ("experiments", "papers") and "project_id" in row:
                row["projects"] = {"user_id": self._admin.owners.get(row["project_id"])}
            self._rows.append(row)
            return FakeResult([row])
        rows = list(self._rows)
        for _, key, value in self._filters:
            rows = [r for r in rows if _path(r, key) == value]
        if self._update is not None:
            for r in rows:
                r.update(self._update)
            return FakeResult(rows)
        if self._order:
            key, desc = self._order
            rows = sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._select and "*" not in self._select:
            cols = [c.strip() for c in self._select.split(",") if "(" not in c and "!" not in c]
            rows = [{k: r.get(k) for k in cols} for r in rows]
        return FakeResult(rows)


class FakeAdmin:
    def __init__(self, tables, owners):
        self.tables = tables
        self.owners = owners

    def table(self, name):
        return FakeQuery(self, name)


def mock_agent_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        card = ReproductionCard.model_validate(body["card"])
        plan = build_plan(card)
        return httpx.Response(200, json=PlanResponse(plan=plan).model_dump(mode="json"))

    return httpx.MockTransport(handler)


@pytest.fixture
def env(monkeypatch):
    card = make_card()
    tables = {
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
            }
        ],
        "experiments": [],
        "experiment_plans": [],
    }
    admin = FakeAdmin(tables, {str(PROJECT_ID): str(USER_ID)})
    monkeypatch.setattr(experiments, "make_service_client", lambda s: admin)
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    app.state.agent_transport = mock_agent_transport()
    app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
    with TestClient(app) as client:
        yield client, admin


def create(client):
    return client.post(f"/v1/papers/{PAPER_ID}/experiments", json={}, headers=AUTH)


def test_create_requires_auth(env):
    client, _ = env
    response = client.post(f"/v1/papers/{PAPER_ID}/experiments", json={})
    assert response.status_code == 401


def test_create_generates_plan_v1(env):
    client, admin = env
    response = create(client)
    assert response.status_code == 201
    detail = response.json()
    assert detail["status"] == "planning"
    assert detail["plan"]["version"] == 1
    assert detail["plan"]["origin"] == "generated"
    assert detail["plan"]["plan"]["model_config"]["architecture"] == "lenet-5"
    assert len(admin.tables["experiment_plans"]) == 1


def test_create_without_card_conflicts(env):
    client, admin = env
    admin.tables["reproduction_specs"] = []
    response = create(client)
    assert response.status_code == 409


def test_foreign_user_cannot_see_experiment(env):
    client, _ = env
    experiment_id = create(client).json()["id"]
    response = client.get(f"/v1/experiments/{experiment_id}", headers=BOB_AUTH)
    assert response.status_code == 404


def test_edit_creates_new_version_preserving_base(env):
    client, admin = env
    experiment_id = create(client).json()["id"]
    response = client.post(
        f"/v1/experiments/{experiment_id}/plans",
        json={
            "base_version": 1,
            "changes": [{"field_path": "training_config.batch_size", "value": 32}],
            "change_reason": "OOM 降低批大小",
            "origin": "oom_retry",
            "related_error": "out of memory",
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    v2 = response.json()
    assert v2["version"] == 2
    assert v2["origin"] == "oom_retry"
    assert v2["related_error"] == "out of memory"
    assert v2["changes"][0]["before"] == 64
    assert v2["changes"][0]["after"] == 32
    assert v2["plan"]["training_config"]["batch_size"]["value"] == 32
    # base version untouched
    v1 = client.get(f"/v1/experiments/{experiment_id}/plans/1", headers=AUTH).json()
    assert v1["plan"]["training_config"]["batch_size"]["value"] == 64
    assert len(admin.tables["experiment_plans"]) == 2


def test_approve_blocked_by_unconfirmed_then_passes(env):
    client, _ = env
    experiment_id = create(client).json()["id"]
    blocked = client.post(f"/v1/experiments/{experiment_id}/plans/1/approve", headers=AUTH)
    assert blocked.status_code == 409
    # confirm both flagged assumptions (nonstandard optimizer + missing loss)
    confirm = client.post(
        f"/v1/experiments/{experiment_id}/plans",
        json={
            "base_version": 1,
            "changes": [
                {"field_path": "training_config.optimizer", "value": "sgd"},
                {"field_path": "training_config.loss_function", "value": "cross_entropy"},
            ],
            "change_reason": "确认关键假设",
        },
        headers=AUTH,
    )
    assert confirm.status_code == 201
    approved = client.post(f"/v1/experiments/{experiment_id}/plans/2/approve", headers=AUTH)
    assert approved.status_code == 200
    assert approved.json()["approved_at"] is not None
    detail = client.get(f"/v1/experiments/{experiment_id}", headers=AUTH).json()
    assert detail["status"] == "approved"


def test_edit_unknown_field_422(env):
    client, _ = env
    experiment_id = create(client).json()["id"]
    response = client.post(
        f"/v1/experiments/{experiment_id}/plans",
        json={
            "base_version": 1,
            "changes": [{"field_path": "model_config.architecture", "value": "resnet"}],
            "change_reason": "x",
        },
        headers=AUTH,
    )
    assert response.status_code == 422


def test_list_plan_versions(env):
    client, _ = env
    experiment_id = create(client).json()["id"]
    client.post(
        f"/v1/experiments/{experiment_id}/plans",
        json={
            "base_version": 1,
            "changes": [{"field_path": "runtime_budget.max_seconds", "value": 300}],
            "change_reason": "缩短预算",
        },
        headers=AUTH,
    )
    versions = client.get(f"/v1/experiments/{experiment_id}/plans", headers=AUTH).json()
    assert [v["version"] for v in versions] == [1, 2]
