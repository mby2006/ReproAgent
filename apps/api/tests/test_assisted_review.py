"""Assisted-review API contracts with in-memory storage and deterministic HTTP advice."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from postgrest.exceptions import APIError
from reproagent_agent.planning import build_plan
from reproagent_api import main as api_main
from reproagent_api.routers import papers
from reproagent_schemas import (
    ExplainResponse,
    FieldExplanation,
    FieldRecommendation,
    PlanResponse,
    RecommendResponse,
    ReproductionCard,
    ReviewRequest,
    preselected_paths,
)
from test_experiments import AUTH, BOB_AUTH, PAPER_ID, USER_ID, FakeQuery, create, make_card
from test_experiments import env as _experiment_env

experiment_env = _experiment_env

OPTIMIZER = "training_config.optimizer"
LOSS = "training_config.loss_function"
WEIGHT_DECAY = "training_config.weight_decay"
CARD_WEIGHT = "hyperparameters.weight_decay"
CARD_SEED = "hyperparameters.seed"
SAFE_ERROR = "AI 辅助暂时不可用，请重试或切换专家模式"
PROVIDER_SECRET = "private-provider-key-and-prompt"


def assisted_card():
    data = make_card().model_dump(mode="json")
    data["optimizer"] = {
        "value": "sgd",
        "status": "inferred",
        "confidence": 0.99,
        "evidence_quote": "The procedure follows the gradient.",
        "source_page": 8,
        "source_section": "Optimization",
    }
    data["hyperparameters"].update(
        {
            "weight_decay": {"value": 0.0001, "status": "inferred", "confidence": 0.94},
            "seed": {"value": 42, "status": "inferred", "confidence": 0.96},
            "epochs": {"value": 20, "status": "inferred", "confidence": 0.65},
        }
    )
    return ReproductionCard.model_validate(data)


class ReviewAgent:
    """Stub HTTP advice with copied context and content-addressed identities."""

    def __init__(self):
        self.calls = []
        self.modifications = {}
        self.confidences = {}
        self.threshold = 0.9
        self.corrupt = None
        self.failure = None
        self.on_review = None
        self.omit_preselection = False

    async def handle(self, request):
        body = json.loads(request.content)
        self.calls.append((request.url.path, deepcopy(body)))
        if request.url.path == "/v1/plan":
            card = ReproductionCard.model_validate(body["card"])
            return httpx.Response(
                200, json=PlanResponse(plan=build_plan(card)).model_dump(mode="json")
            )
        assert request.url.path in {"/v1/explain", "/v1/recommend"}
        if self.failure == "timeout":
            raise httpx.ReadTimeout(PROVIDER_SECRET, request=request)
        if self.failure == "http":
            return httpx.Response(503, text=PROVIDER_SECRET)
        context = ReviewRequest.model_validate(body)
        fields = context.review_fields()
        selected = preselected_paths(fields, self.threshold)
        if self.omit_preselection:
            selected = []
        if request.url.path == "/v1/recommend":
            recommendations = []
            for field in fields:
                modifying = field.field_path in self.modifications
                advice = {
                    "field_path": field.field_path,
                    "action": "modify" if modifying else "accept",
                    "suggested_value": self.modifications.get(field.field_path, field.value),
                    "confidence": self.confidences.get(field.field_path, 0.98),
                    "reasoning": f"Baseline advice for {field.field_path}",
                    "risk": "This inference is not a claim about the paper.",
                }
                identity = context.context_hash() + json.dumps(advice, sort_keys=True)
                recommendations.append(
                    FieldRecommendation(
                        id=hashlib.sha256(identity.encode()).hexdigest(),
                        **advice,
                    )
                )
            selected = [
                item.field_path
                for item in recommendations
                if item.field_path in selected
                and item.action == "accept"
                and item.confidence >= self.threshold
            ]
            result = RecommendResponse(
                context_hash=context.context_hash(),
                recommendations=recommendations,
                preselected_paths=selected,
                selection_threshold=self.threshold,
            )
        else:
            result = ExplainResponse(
                context_hash=context.context_hash(),
                explanations=[
                    FieldExplanation(
                        field_path=field.field_path,
                        what=f"Meaning of {field.field_path}",
                        why="The plan needs a concrete choice.",
                        consequence="Affects execution.",
                        risk="An inference is not paper evidence.",
                    )
                    for field in fields
                ],
                preselected_paths=selected,
                selection_threshold=self.threshold,
            )
        payload = deepcopy(result.model_dump(mode="json"))
        if self.on_review is not None:
            callback, self.on_review = self.on_review, None
            await callback()
        if self.corrupt is not None:
            payload = self.corrupt(payload)
        if payload is None:
            return httpx.Response(200, text=PROVIDER_SECRET)
        return httpx.Response(200, json=deepcopy(payload))


@pytest.fixture(autouse=True)
def offline_redis(monkeypatch):
    async def no_pool(*args, **kwargs):
        return None

    monkeypatch.setattr(api_main, "create_pool", no_pool)


@pytest.fixture
def review_env(experiment_env, monkeypatch):
    client, admin = experiment_env
    monkeypatch.setattr(papers, "make_service_client", lambda settings: admin)
    admin.tables["reproduction_specs"][0].update(
        {
            "spec_json": assisted_card().model_dump(mode="json"),
            "origin": "extraction",
            "parent_version_id": None,
            "status": "draft",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    agent = ReviewAgent()
    client.app.state.agent_transport = httpx.MockTransport(agent.handle)
    return SimpleNamespace(client=client, admin=admin, agent=agent)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def new_experiment(env):
    return ok(create(env.client), 201)


def plan_url(experiment, version=1):
    return f"/v1/experiments/{experiment['id']}/plans/{version}"


def card_url():
    return f"/v1/papers/{PAPER_ID}/card"


def recommend(env, experiment, version=1):
    return ok(env.client.post(plan_url(experiment, version) + "/recommend", headers=AUTH))


def adoption_body(advice, paths=None, **extra):
    return {
        "context_hash": advice["context_hash"],
        "recommendation_ids": [
            item["id"]
            for item in advice["recommendations"]
            if paths is None or item["field_path"] in paths
        ],
        **extra,
    }


def explain_card(env, version=1, **extra):
    return ok(
        env.client.post(
            card_url() + "/explain",
            json={"base_version": version, **extra},
            headers=AUTH,
        )
    )


def card_batch_body(advice, paths=None, version=1, **extra):
    return {
        "base_version": version,
        "context_hash": advice["context_hash"],
        "field_paths": [CARD_WEIGHT] if paths is None else paths,
        **extra,
    }


def assert_audit(record, recommendation, before, after, started, threshold=0.9):
    assert record["before"] == before
    assert record["after"] == after
    assert record["confirmed_by"] == "ai_assisted"
    assert record["review_mode"] == "assisted"
    assert record["actor"] == str(USER_ID)
    timestamp = datetime.fromisoformat(record["timestamp"])
    assert started <= timestamp <= datetime.now(UTC)
    assert record["recommendation_id"] == recommendation["id"]
    assert record["recommendation_confidence"] == recommendation["confidence"]
    assert record["recommendation_reasoning"] == recommendation["reasoning"]
    assert record["recommendation_risk"] == recommendation["risk"]
    assert record["preselection_threshold"] == threshold


@pytest.mark.parametrize("headers,expected", [({}, 401), (BOB_AUTH, 404)])
@pytest.mark.parametrize(
    "route",
    [
        "plan-explain",
        "plan-recommend",
        "plan-adopt",
        "plan-approve",
        "card-explain",
        "card-batch",
        "card-confirm",
    ],
)
def test_auth_and_ownership_are_checked_before_agent_or_writes(
    review_env, headers, expected, route
):
    env = review_env
    experiment = new_experiment(env)
    routes = {
        "plan-explain": (plan_url(experiment) + "/explain", {}),
        "plan-recommend": (plan_url(experiment) + "/recommend", None),
        "plan-adopt": (
            plan_url(experiment) + "/adopt",
            {
                "context_hash": "a" * 64,
                "recommendation_ids": ["unknown"],
            },
        ),
        "plan-approve": (plan_url(experiment) + "/approve", {"review_mode": "expert"}),
        "card-explain": (card_url() + "/explain", {"base_version": 1}),
        "card-batch": (
            card_url() + "/confirm-batch",
            {
                "base_version": 1,
                "context_hash": "a" * 64,
                "field_paths": [CARD_WEIGHT],
            },
        ),
        "card-confirm": (card_url() + "/confirm", {"review_mode": "expert"}),
    }
    env.agent.calls.clear()
    before = deepcopy(env.admin.tables)
    url, body = routes[route]
    response = env.client.post(url, json=body, headers=headers)
    assert response.status_code == expected, response.text
    assert env.agent.calls == []
    assert env.admin.tables == before


def test_generation_uses_pinned_card_not_latest_and_never_writes(review_env):
    env = review_env
    experiment = new_experiment(env)
    base = deepcopy(experiment["plan"])
    pinned_card = deepcopy(env.admin.tables["reproduction_specs"][0]["spec_json"])
    newer = ok(
        env.client.post(
            card_url() + "/edits",
            headers=AUTH,
            json={
                "base_version": 1,
                "edits": [{"field_path": "optimizer", "value": "adam"}],
                "review_mode": "expert",
            },
        ),
        201,
    )
    assert newer["card"]["optimizer"]["value"] == "adam"
    before = deepcopy(env.admin.tables)
    env.agent.calls.clear()
    explanation = ok(
        env.client.post(
            plan_url(experiment) + "/explain",
            headers=AUTH,
            json={
                "field_paths": [OPTIMIZER],
            },
        )
    )
    advice = recommend(env, experiment)
    assert [item["field_path"] for item in explanation["explanations"]] == [OPTIMIZER]
    assert {item["field_path"] for item in advice["recommendations"]} == {
        OPTIMIZER,
        LOSS,
        WEIGHT_DECAY,
    }
    assert advice["preselected_paths"] == [WEIGHT_DECAY]
    assert explanation["context_hash"] == advice["context_hash"]
    assert env.agent.calls[0][1]["field_paths"] == [OPTIMIZER]
    for _, body in env.agent.calls:
        assert body["card"] == pinned_card
        assert body["plan"] == base["plan"]
        assert body["scope"] == str(USER_ID)
    assert env.admin.tables == before
    assert ok(env.client.get(plan_url(experiment), headers=AUTH)) == base
    assert env.client.post(plan_url(experiment) + "/approve", headers=AUTH).status_code == 409


@pytest.mark.parametrize("route", ["plan-explain", "card-explain"])
@pytest.mark.parametrize("kind", ["unknown", "duplicate", "already-explicit"])
def test_explain_only_accepts_requested_pending_fields(review_env, route, kind):
    env = review_env
    experiment = new_experiment(env)
    is_plan = route == "plan-explain"
    pending = OPTIMIZER if is_plan else "optimizer"
    paths = {
        "unknown": ["unknown.field"],
        "duplicate": [pending, pending],
        "already-explicit": ["training_config.learning_rate" if is_plan else "dataset"],
    }[kind]
    body = {"field_paths": paths, **({} if is_plan else {"base_version": 1})}
    url = plan_url(experiment) if is_plan else card_url()
    env.agent.calls.clear()
    before = deepcopy(env.admin.tables)
    response = env.client.post(url + "/explain", json=body, headers=AUTH)
    assert response.status_code == 422, response.text
    assert env.agent.calls == []
    assert env.admin.tables == before


def corrupt_output(payload, kind):
    key = "recommendations" if "recommendations" in payload else "explanations"
    items = payload[key]
    if kind == "not-json":
        return None
    if kind == "hash":
        payload["context_hash"] = "b" * 64
    elif kind == "duplicate-fields":
        items.append(deepcopy(items[0]))
    elif kind == "missing-field":
        items.pop()
    elif kind == "unknown-field":
        items[0]["field_path"] = "invented.field"
    elif kind == "extra-evidence":
        items[0]["evidence_quote"] = PROVIDER_SECRET
    elif kind == "critical-preselection":
        payload["preselected_paths"] = [items[0]["field_path"]]
    elif kind == "duplicate-ids":
        items[1]["id"] = items[0]["id"]
    elif kind == "changed-accept":
        items[0]["suggested_value"] = "adam"
    elif kind == "invalid-confidence":
        items[0]["confidence"] = 1.1
    elif kind == "structured-value":
        items[0]["suggested_value"] = {"value": "adam", "quote": PROVIDER_SECRET}
    elif kind == "invalid-action":
        items[0]["action"] = "auto_confirm"
    elif kind == "modified-preselection":
        item = next(item for item in items if item["field_path"] == WEIGHT_DECAY)
        item.update(action="modify", suggested_value=0.002)
    elif kind == "low-advice-preselection":
        next(item for item in items if item["field_path"] == WEIGHT_DECAY)["confidence"] = 0.2
    elif kind == "bool-accept":
        next(item for item in items if item["field_path"] == WEIGHT_DECAY)["suggested_value"] = (
            False
        )
    return payload


@pytest.mark.parametrize("route", ["plan-explain", "plan-recommend", "card-explain"])
@pytest.mark.parametrize(
    "kind",
    [
        "not-json",
        "hash",
        "duplicate-fields",
        "missing-field",
        "unknown-field",
        "extra-evidence",
        "critical-preselection",
    ],
)
def test_invalid_agent_output_returns_safe_502_without_writes(review_env, route, kind):
    env = review_env
    experiment = new_experiment(env)
    env.agent.corrupt = lambda payload: corrupt_output(payload, kind)
    if route == "card-explain":
        url, body = card_url() + "/explain", {"base_version": 1}
    else:
        url = plan_url(experiment) + ("/recommend" if route == "plan-recommend" else "/explain")
        body = {} if route == "plan-explain" else None
    before = deepcopy(env.admin.tables)
    response = env.client.post(url, json=body, headers=AUTH)
    assert response.status_code == 502, response.text
    assert response.json() == {"detail": SAFE_ERROR}
    assert PROVIDER_SECRET not in response.text
    assert env.admin.tables == before


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate-ids",
        "changed-accept",
        "invalid-confidence",
        "structured-value",
        "invalid-action",
        "modified-preselection",
        "low-advice-preselection",
        "bool-accept",
    ],
)
def test_invalid_recommendations_cannot_be_offered_for_adoption(review_env, kind):
    env = review_env
    if kind == "bool-accept":
        env.admin.tables["reproduction_specs"][0]["spec_json"]["hyperparameters"]["weight_decay"][
            "value"
        ] = 0.0
    experiment = new_experiment(env)
    before = deepcopy(env.admin.tables)
    env.agent.corrupt = lambda payload: corrupt_output(payload, kind)
    response = env.client.post(plan_url(experiment) + "/recommend", headers=AUTH)
    assert response.status_code == 502, response.text
    assert response.json() == {"detail": SAFE_ERROR}
    assert env.admin.tables == before


@pytest.mark.parametrize("is_card", [False, True])
def test_explanation_must_not_return_extra_unrequested_pending_fields(review_env, is_card):
    env = review_env
    experiment = new_experiment(env)
    url = card_url() if is_card else plan_url(experiment)
    pending = "optimizer" if is_card else OPTIMIZER

    def extra_field(payload):
        extra = deepcopy(payload["explanations"][0])
        extra["field_path"] = "loss_function" if is_card else LOSS
        payload["explanations"].append(extra)
        return payload

    env.agent.corrupt = extra_field
    before = deepcopy(env.admin.tables)
    response = env.client.post(
        url + "/explain",
        headers=AUTH,
        json={
            "field_paths": [pending],
            **({"base_version": 1} if is_card else {}),
        },
    )
    assert response.status_code == 502, response.text
    assert response.json() == {"detail": SAFE_ERROR}
    assert env.admin.tables == before


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_provider_failure_does_not_prevent_expert_editing_and_approval(review_env, failure):
    env = review_env
    experiment = new_experiment(env)
    env.agent.failure = failure
    response = env.client.post(plan_url(experiment) + "/recommend", headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": SAFE_ERROR}
    response = env.client.post(card_url() + "/explain", json={"base_version": 1}, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": SAFE_ERROR}
    env.agent.calls.clear()
    edited = ok(
        env.client.post(
            f"/v1/experiments/{experiment['id']}/plans",
            headers=AUTH,
            json={
                "base_version": 1,
                "review_mode": "expert",
                "change_reason": "Manual review",
                "changes": [
                    {"field_path": OPTIMIZER, "value": "sgd"},
                    {"field_path": LOSS, "value": "cross_entropy"},
                ],
            },
        ),
        201,
    )
    assert edited["parent_version_id"] == experiment["plan"]["id"]
    for name in ["optimizer", "loss_function"]:
        param = edited["plan"]["training_config"][name]
        assert param["source"] == "human_confirmed"
        assert param["confirmed_by"] == "human"
    for record in edited["changes"]:
        assert record["actor"] == str(USER_ID)
        assert record["review_mode"] == "expert"
    approved = ok(env.client.post(plan_url(experiment, 2) + "/approve", headers=AUTH))
    assert approved["approved_at"] is not None
    assert approved["plan"]["review_mode"] == "expert"
    card = ok(
        env.client.post(
            card_url() + "/edits",
            headers=AUTH,
            json={
                "base_version": 1,
                "review_mode": "expert",
                "edits": [{"field_path": "optimizer", "value": "adam"}],
            },
        ),
        201,
    )
    assert card["card"]["optimizer"]["confirmed_by"] == "human"
    confirmed = ok(env.client.post(card_url() + "/confirm", headers=AUTH), 201)
    assert confirmed["card"]["review_mode"] == "expert"
    assert env.agent.calls == []


@pytest.mark.parametrize("action,value", [("accept", "sgd"), ("modify", "adam")])
def test_individual_adoption_is_versioned_audited_and_keeps_remaining_critical_gate(
    review_env,
    action,
    value,
):
    env = review_env
    experiment = new_experiment(env)
    if action == "modify":
        env.agent.modifications[OPTIMIZER] = value
    advice = recommend(env, experiment)
    base = deepcopy(experiment["plan"])
    original = deepcopy(base["plan"]["training_config"]["optimizer"])
    started = datetime.now(UTC)
    adopted = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(
                advice,
                [OPTIMIZER],
            ),
        ),
        201,
    )
    assert adopted["version"] == 2
    assert adopted["parent_version_id"] == base["id"]
    assert adopted["origin"] == "user_edit"
    assert adopted["approved_at"] is None
    param = adopted["plan"]["training_config"]["optimizer"]
    assert param["value"] == value
    assert param["source"] == "inferred"
    assert param["human_confirmed"] is True
    assert param["confirmed_by"] == "ai_assisted"
    assert param["requires_confirmation"] is False
    for key in ["paper_value", "evidence_quote", "source_page", "confidence"]:
        assert param[key] == original[key]
    assert param["confidence"] == 0.99
    item = next(item for item in advice["recommendations"] if item["field_path"] == OPTIMIZER)
    record = next(record for record in adopted["changes"] if record["field_path"] == OPTIMIZER)
    assert_audit(record, item, "sgd", value, started)
    assert record["recommendation_confidence"] == 0.98
    assumption = next(a for a in adopted["plan"]["assumptions"] if a["field_path"] == OPTIMIZER)
    assert assumption["human_confirmed"] is True
    assert assumption["confirmed_by"] == "ai_assisted"
    assert assumption["kind"] == "inferred"
    assert assumption["confidence"] == 0.99
    blocked = env.client.post(plan_url(experiment, 2) + "/approve", headers=AUTH)
    assert blocked.status_code == 409
    assert LOSS in blocked.json()["detail"]
    assert ok(env.client.get(plan_url(experiment), headers=AUTH)) == base
    assert (
        ok(env.client.get(f"/v1/experiments/{experiment['id']}", headers=AUTH))["status"]
        == "planning"
    )
    calls_before = len(env.agent.calls)
    repeated = env.client.post(
        plan_url(experiment) + "/adopt",
        headers=AUTH,
        json=adoption_body(
            advice,
            [OPTIMIZER],
        ),
    )
    assert repeated.status_code == 409
    assert len(env.agent.calls) == calls_before
    assert len(env.admin.tables["experiment_plans"]) == 2


def test_remaining_advice_is_regenerated_for_child_and_only_then_clears_gate(review_env):
    env = review_env
    experiment = new_experiment(env)
    first_advice = recommend(env, experiment)
    child = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(first_advice, [OPTIMIZER]),
        ),
        201,
    )
    remaining = recommend(env, experiment, 2)
    assert {item["field_path"] for item in remaining["recommendations"]} == {LOSS, WEIGHT_DECAY}
    assert remaining["context_hash"] != first_advice["context_hash"]
    grandchild = ok(
        env.client.post(
            plan_url(experiment, 2) + "/adopt",
            headers=AUTH,
            json=adoption_body(remaining),
        ),
        201,
    )
    assert grandchild["version"] == 3
    assert grandchild["parent_version_id"] == child["id"]
    assert (
        grandchild["plan"]["training_config"]["optimizer"]
        == (child["plan"]["training_config"]["optimizer"])
    )
    assert grandchild["approved_at"] is None
    assert ok(env.client.get(plan_url(experiment, 2), headers=AUTH)) == child
    assert ok(env.client.post(plan_url(experiment, 3) + "/approve", headers=AUTH))["approved_at"]


def test_recommendation_confidence_cannot_replace_extraction_confidence(review_env):
    env = review_env
    env.admin.tables["reproduction_specs"][0]["spec_json"]["hyperparameters"]["weight_decay"][
        "confidence"
    ] = 0.4
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    assert advice["preselected_paths"] == []
    item = next(item for item in advice["recommendations"] if item["field_path"] == WEIGHT_DECAY)
    assert item["confidence"] == 0.98

    def invalid_preselection(payload):
        payload["preselected_paths"] = [WEIGHT_DECAY]
        return payload

    env.agent.corrupt = invalid_preselection
    before = deepcopy(env.admin.tables)
    response = env.client.post(plan_url(experiment) + "/recommend", headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": SAFE_ERROR}
    assert env.admin.tables == before
    env.agent.corrupt = None
    # An explicit individual choice remains allowed, but is never extraction evidence.
    adopted = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(advice, [WEIGHT_DECAY]),
        ),
        201,
    )
    param = adopted["plan"]["training_config"]["weight_decay"]
    assert param["confidence"] == 0.4
    assert param["source"] == "inferred"
    assert param["confirmed_by"] == "ai_assisted"
    assert adopted["changes"][0]["recommendation_confidence"] == 0.98


def test_failed_server_revalidation_during_adoption_does_not_write(review_env):
    env = review_env
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    env.agent.corrupt = lambda payload: corrupt_output(payload, "hash")
    before = deepcopy(env.admin.tables)
    response = env.client.post(
        plan_url(experiment) + "/adopt",
        headers=AUTH,
        json=adoption_body(advice),
    )
    assert response.status_code == 502
    assert response.json() == {"detail": SAFE_ERROR}
    assert env.admin.tables == before


def test_batch_adoption_and_approval_are_two_distinct_decisions(review_env):
    env = review_env
    experiment = new_experiment(env)
    env.agent.threshold = 0.93
    advice = recommend(env, experiment)
    started = datetime.now(UTC)
    adopted = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(advice),
        ),
        201,
    )
    assert adopted["approved_at"] is None
    assert adopted["version"] == 2
    assert adopted["parent_version_id"] == experiment["plan"]["id"]
    by_path = {item["field_path"]: item for item in advice["recommendations"]}
    for record in adopted["changes"]:
        item = by_path[record["field_path"]]
        assert_audit(record, item, item["suggested_value"], item["suggested_value"], started, 0.93)
    loss = adopted["plan"]["training_config"]["loss_function"]
    assert loss["source"] == "inferred"
    assert loss["evidence_quote"] is None
    assert loss["source_page"] is None
    assert loss["confidence"] == 0
    assert adopted["plan"]["training_config"]["weight_decay"]["confidence"] == 0.94
    assert (
        ok(env.client.get(f"/v1/experiments/{experiment['id']}", headers=AUTH))["status"]
        == "planning"
    )
    calls_before = len(env.agent.calls)
    approved = ok(env.client.post(plan_url(experiment, 2) + "/approve", headers=AUTH))
    assert approved["version"] == 2
    assert approved["approved_at"] is not None
    assert len(env.agent.calls) == calls_before
    assert (
        ok(env.client.get(f"/v1/experiments/{experiment['id']}", headers=AUTH))["status"]
        == "approved"
    )


@pytest.mark.parametrize("mode", ["expert", "assisted"])
def test_approval_records_mode_in_new_version_only_when_changed(review_env, mode):
    env = review_env
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    base = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(advice),
        ),
        201,
    )
    started = datetime.now(UTC)
    approved = ok(
        env.client.post(
            plan_url(experiment, 2) + "/approve",
            headers=AUTH,
            json={"review_mode": mode},
        )
    )
    assert approved["plan"]["review_mode"] == mode
    assert approved["approved_at"] is not None
    assert approved["version"] == (3 if mode == "expert" else 2)
    if mode == "expert":
        assert approved["parent_version_id"] == base["id"]
        record = approved["changes"][0]
        assert record["field_path"] == "review_mode"
        assert (record["before"], record["after"]) == ("assisted", "expert")
        assert record["actor"] == str(USER_ID)
        assert started <= datetime.fromisoformat(record["timestamp"]) <= datetime.now(UTC)
        assert ok(env.client.get(plan_url(experiment, 2), headers=AUTH)) == base


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("hash", 409),
        ("unknown-id", 409),
        ("duplicate-ids", 422),
        ("value-override", 422),
        ("changes-override", 422),
        ("auto-mode", 422),
        ("delegated-mode", 422),
        ("changed-server-advice", 409),
    ],
)
def test_adoption_rejects_stale_ids_context_and_client_value_overrides(review_env, kind, expected):
    env = review_env
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    body = adoption_body(advice, [OPTIMIZER])
    if kind == "hash":
        body["context_hash"] = "b" * 64
    elif kind == "unknown-id":
        body["recommendation_ids"] = ["not-generated-on-server"]
    elif kind == "duplicate-ids":
        body["recommendation_ids"] *= 2
    elif kind == "value-override":
        body["suggested_value"] = "adam"
    elif kind == "changes-override":
        body["changes"] = [{"field_path": OPTIMIZER, "value": "adam"}]
    elif kind.endswith("-mode"):
        body["review_mode"] = kind.removesuffix("-mode")
    else:
        env.agent.modifications[OPTIMIZER] = "adam"
    before = deepcopy(env.admin.tables)
    calls_before = len(env.agent.calls)
    response = env.client.post(plan_url(experiment) + "/adopt", json=body, headers=AUTH)
    assert response.status_code == expected, response.text
    assert env.admin.tables == before
    if kind == "hash" or expected == 422:
        assert len(env.agent.calls) == calls_before


def test_old_advice_cannot_be_applied_to_a_new_plan_context(review_env):
    env = review_env
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    ok(
        env.client.post(
            f"/v1/experiments/{experiment['id']}/plans",
            headers=AUTH,
            json={
                "base_version": 1,
                "changes": [{"field_path": "runtime_budget.max_seconds", "value": 100}],
                "change_reason": "Budget changed",
            },
        ),
        201,
    )
    fresh = recommend(env, experiment, 2)
    assert fresh["context_hash"] != advice["context_hash"]
    before = deepcopy(env.admin.tables)
    for version, body in [
        (1, adoption_body(advice)),
        (2, adoption_body(advice)),
        (2, {**adoption_body(advice), "context_hash": fresh["context_hash"]}),
    ]:
        response = env.client.post(
            plan_url(experiment, version) + "/adopt", json=body, headers=AUTH
        )
        assert response.status_code == 409, response.text
    assert env.admin.tables == before


def concurrent_plan(env):
    base = env.admin.tables["experiment_plans"][0]
    winner = deepcopy(base)
    winner.update(id=str(uuid4()), version=2, parent_version_id=base["id"], origin="user_edit")
    winner["config_json"]["runtime_budget"]["max_seconds"] = 321
    env.admin.tables["experiment_plans"].append(winner)
    return deepcopy(winner)


@pytest.mark.parametrize("race", ["during-agent-await", "unique-insert"])
def test_adoption_rejects_concurrent_plan_instead_of_creating_a_branch(
    review_env, monkeypatch, race
):
    env = review_env
    experiment = new_experiment(env)
    advice = recommend(env, experiment)
    base = deepcopy(env.admin.tables["experiment_plans"][0])
    winners = []
    collisions = []
    if race == "during-agent-await":

        async def insert_while_awaiting():
            winners.append(concurrent_plan(env))

        env.agent.on_review = insert_while_awaiting
    else:

        class RacingQuery(FakeQuery):
            def execute(self):
                if self._name == "experiment_plans" and self._insert is not None and not collisions:
                    collisions.append(self._insert["version"])
                    winners.append(concurrent_plan(env))
                    raise APIError(
                        {
                            "code": "23505",
                            "message": "duplicate experiment/version",
                            "details": None,
                            "hint": None,
                        }
                    )
                return super().execute()

        monkeypatch.setattr(env.admin, "table", lambda name: RacingQuery(env.admin, name))
    response = env.client.post(
        plan_url(experiment) + "/adopt",
        headers=AUTH,
        json=adoption_body(advice),
    )
    assert response.status_code == 409, response.text
    assert len(winners) == 1
    assert env.admin.tables["experiment_plans"] == [base, winners[0]]
    if race == "unique-insert":
        assert collisions == [2]
    versions = ok(env.client.get(f"/v1/experiments/{experiment['id']}/plans", headers=AUTH))
    assert [item["version"] for item in versions] == [1, 2]


def test_unconfirm_after_ai_adoption_resets_confirmation_and_critical_gate(review_env):
    env = review_env
    experiment = new_experiment(env)
    adopted = ok(
        env.client.post(
            plan_url(experiment) + "/adopt",
            headers=AUTH,
            json=adoption_body(recommend(env, experiment)),
        ),
        201,
    )
    reset = ok(
        env.client.post(
            f"/v1/experiments/{experiment['id']}/plans",
            headers=AUTH,
            json={
                "base_version": 2,
                "review_mode": "expert",
                "change_reason": "Needs another review",
                "changes": [{"field_path": OPTIMIZER, "value": "sgd", "human_confirmed": False}],
            },
        ),
        201,
    )
    assert reset["parent_version_id"] == adopted["id"]
    param = reset["plan"]["training_config"]["optimizer"]
    assert param["source"] == "inferred"
    assert param["human_confirmed"] is False
    assert param["confirmed_by"] is None
    assert param["requires_confirmation"] is True
    assumption = next(a for a in reset["plan"]["assumptions"] if a["field_path"] == OPTIMIZER)
    assert assumption["confirmed_by"] is None
    assert assumption["human_confirmed"] is False
    assert assumption["requires_confirmation"] is True
    assert env.client.post(plan_url(experiment, 3) + "/approve", headers=AUTH).status_code == 409
    assert ok(env.client.get(plan_url(experiment, 2), headers=AUTH)) == adopted


def test_card_explain_preselects_but_does_not_confirm_or_mutate(review_env):
    env = review_env
    before = deepcopy(env.admin.tables)
    explanation = explain_card(env)
    assert explanation["preselected_paths"] == [CARD_WEIGHT, CARD_SEED]
    subset = explain_card(env, field_paths=[CARD_SEED])
    assert [item["field_path"] for item in subset["explanations"]] == [CARD_SEED]
    assert subset["preselected_paths"] == [CARD_SEED]
    assert subset["context_hash"] == explanation["context_hash"]
    assert env.admin.tables == before
    current = ok(env.client.get(card_url(), headers=AUTH))
    assert current["version"] == 1
    assert current["card"]["hyperparameters"]["seed"]["human_confirmed"] is False
    assert current["card"]["hyperparameters"]["seed"]["confirmed_by"] is None


@pytest.mark.parametrize("base_status", ["draft", "confirmed"])
def test_card_batch_can_uncheck_subset_and_preserves_base_status_and_evidence(
    review_env, base_status
):
    env = review_env
    env.admin.tables["reproduction_specs"][0]["status"] = base_status
    base = ok(env.client.get(card_url(), headers=AUTH))
    env.agent.threshold = 0.93
    advice = explain_card(env)
    selected = ok(
        env.client.post(
            card_url() + "/confirm-batch",
            headers=AUTH,
            json=card_batch_body(
                advice,
                review_mode="expert",
            ),
        ),
        201,
    )
    assert selected["version"] == 2
    assert selected["parent_version_id"] == base["id"]
    assert selected["card"]["review_mode"] == "expert"
    assert selected["card"]["review_selection_threshold"] == 0.93
    field = selected["card"]["hyperparameters"]["weight_decay"]
    original = base["card"]["hyperparameters"]["weight_decay"]
    assert field == {**original, "human_confirmed": True, "confirmed_by": "ai_assisted"}
    assert selected["card"]["hyperparameters"]["seed"]["human_confirmed"] is False
    assert selected["card"]["optimizer"] == base["card"]["optimizer"]
    assert selected["card"]["dataset"] == base["card"]["dataset"]
    assert selected["card"]["loss_function"] == base["card"]["loss_function"]
    assert env.admin.tables["reproduction_specs"][-1]["status"] == base_status
    assert ok(env.client.get(card_url() + "?version=1", headers=AUTH)) == base
    calls_before = len(env.agent.calls)
    repeated = env.client.post(
        card_url() + "/confirm-batch", headers=AUTH, json=card_batch_body(advice)
    )
    assert repeated.status_code == 409
    assert len(env.agent.calls) == calls_before
    confirmed = ok(env.client.post(card_url() + "/confirm", headers=AUTH), 201)
    assert confirmed["version"] == 3
    assert confirmed["parent_version_id"] == selected["id"]
    assert confirmed["card"]["review_mode"] == "expert"
    assert env.admin.tables["reproduction_specs"][-1]["status"] == "confirmed"


@pytest.mark.parametrize(
    "paths",
    [
        ["optimizer"],
        ["hyperparameters.epochs"],
        ["loss_function"],
        ["hyperparameters.batch_size"],
        [CARD_WEIGHT, CARD_WEIGHT],
        ["unknown.field"],
        [CARD_WEIGHT, "optimizer"],
    ],
)
def test_card_batch_rejects_critical_low_confidence_missing_and_unselected_fields(
    review_env, paths
):
    env = review_env
    advice = explain_card(env)
    before = deepcopy(env.admin.tables)
    response = env.client.post(
        card_url() + "/confirm-batch",
        headers=AUTH,
        json=card_batch_body(advice, paths),
    )
    assert response.status_code == 422, response.text
    assert env.admin.tables == before


def test_card_batch_requires_server_preselection_not_just_high_confidence(review_env):
    env = review_env
    env.agent.omit_preselection = True
    advice = explain_card(env)
    assert advice["preselected_paths"] == []
    before = deepcopy(env.admin.tables)
    response = env.client.post(
        card_url() + "/confirm-batch", headers=AUTH, json=card_batch_body(advice)
    )
    assert response.status_code == 422
    assert env.admin.tables == before


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({"context_hash": "b" * 64}, 409),
        ({"value": 999}, 422),
        ({"edits": [{"field_path": CARD_WEIGHT, "value": 999}]}, 422),
        ({"review_mode": "auto"}, 422),
        ({"review_mode": "delegated"}, 422),
    ],
)
def test_card_batch_rejects_stale_hash_and_client_overrides(review_env, extra, expected):
    env = review_env
    advice = explain_card(env)
    before = deepcopy(env.admin.tables)
    calls_before = len(env.agent.calls)
    response = env.client.post(
        card_url() + "/confirm-batch",
        headers=AUTH,
        json={**card_batch_body(advice), **extra},
    )
    assert response.status_code == expected, response.text
    assert len(env.agent.calls) == calls_before
    assert env.admin.tables == before


def test_card_context_is_stale_after_versioned_edit_even_with_latest_base_number(review_env):
    env = review_env
    advice = explain_card(env)
    ok(
        env.client.post(
            card_url() + "/edits",
            headers=AUTH,
            json={
                "base_version": 1,
                "edits": [{"field_path": CARD_SEED, "value": 100}],
            },
        ),
        201,
    )
    before = deepcopy(env.admin.tables)
    calls_before = len(env.agent.calls)
    for version in [1, 2]:
        response = env.client.post(
            card_url() + "/confirm-batch",
            headers=AUTH,
            json=card_batch_body(
                advice,
                version=version,
            ),
        )
        assert response.status_code == 409, response.text
    assert len(env.agent.calls) == calls_before
    assert env.admin.tables == before


def test_card_confirm_records_requested_mode_without_overwriting_base(review_env):
    env = review_env
    before = ok(env.client.get(card_url(), headers=AUTH))
    confirmed = ok(
        env.client.post(
            card_url() + "/confirm",
            headers=AUTH,
            json={"review_mode": "expert"},
        ),
        201,
    )
    assert confirmed["version"] == 2
    assert confirmed["parent_version_id"] == before["id"]
    assert confirmed["card"]["review_mode"] == "expert"
    assert confirmed["card"]["optimizer"]["human_confirmed"] is False
    assert ok(env.client.get(card_url() + "?version=1", headers=AUTH)) == before
    assert env.agent.calls == []


def test_card_unconfirm_resets_ai_source_without_changing_evidence(review_env):
    env = review_env
    selected = ok(
        env.client.post(
            card_url() + "/confirm-batch",
            headers=AUTH,
            json=card_batch_body(
                explain_card(env),
            ),
        ),
        201,
    )
    field = selected["card"]["hyperparameters"]["weight_decay"]
    reset = ok(
        env.client.post(
            card_url() + "/edits",
            headers=AUTH,
            json={
                "base_version": 2,
                "review_mode": "expert",
                "edits": [
                    {
                        "field_path": CARD_WEIGHT,
                        "value": field["value"],
                        "human_confirmed": False,
                    }
                ],
            },
        ),
        201,
    )
    assert reset["parent_version_id"] == selected["id"]
    assert reset["card"]["hyperparameters"]["weight_decay"] == {
        **field,
        "human_confirmed": False,
        "confirmed_by": None,
    }
    assert ok(env.client.get(card_url() + "?version=2", headers=AUTH)) == selected


def delegate_plan(env, experiment, version=1, **body):
    return env.client.post(
        plan_url(experiment, version) + "/delegate", headers=AUTH,
        json={"base_version": version, "consent": True, **body},
    )


def test_delegation_accepts_and_approves_without_faking_human_review(review_env):
    env = review_env
    experiment = new_experiment(env)
    original = ok(env.client.get(plan_url(experiment), headers=AUTH))
    env.agent.modifications[WEIGHT_DECAY] = 0.0
    accepted = ok(delegate_plan(env, experiment), 201)
    assert accepted["version"] == 2
    assert accepted["parent_version_id"] == original["id"]
    assert accepted["approved_at"] is not None
    plan = accepted["plan"]
    assert plan["review_mode"] == "delegated"
    assert plan["delegated_by"] == str(USER_ID)
    assert plan["delegated_at"]
    pending = [a for a in plan["assumptions"] if a["requires_confirmation"]]
    assert pending
    assert all(a["confirmed_by"] == "auto" and not a["human_confirmed"] for a in pending)
    optimizer = plan["training_config"]["optimizer"]
    assert optimizer["source"] == "inferred"
    assert optimizer["human_confirmed"] is False
    assert optimizer["confirmed_by"] == "auto"
    original_optimizer = original["plan"]["training_config"]["optimizer"]
    assert optimizer["evidence_quote"] == original_optimizer["evidence_quote"]
    loss = plan["training_config"]["loss_function"]
    assert loss["evidence_quote"] is None
    assert loss["source_page"] is None
    assert plan["training_config"]["weight_decay"]["value"] == 0.0
    events = accepted["changes"]
    assert any(e["field_path"] == "review_mode" for e in events)
    assert all(e["id"] and e["actor"] == str(USER_ID) and e["timestamp"] for e in events)
    assert ok(env.client.get(plan_url(experiment), headers=AUTH)) == original
    assert delegate_plan(env, experiment).status_code == 409
    approved_again = ok(env.client.post(plan_url(experiment, 2) + "/approve", headers=AUTH))
    assert approved_again["id"] == accepted["id"]


@pytest.mark.parametrize("body", [
    {"base_version": 1}, {"base_version": 1, "consent": False},
    {"base_version": 1, "consent": True, "delegated_by": str(uuid4())},
    {"base_version": 1, "consent": True, "delegated_at": "2026-01-01T00:00:00Z"},
])
def test_delegation_requires_explicit_unforgeable_consent(review_env, body):
    env = review_env
    experiment = new_experiment(env)
    before = deepcopy(env.admin.tables)
    calls = len(env.agent.calls)
    for url in (card_url(), plan_url(experiment)):
        response = env.client.post(url + "/delegate", headers=AUTH, json=body)
        assert response.status_code == 422, response.text
    assert env.admin.tables == before
    assert len(env.agent.calls) == calls


def test_delegation_checks_ownership_before_model_or_writes(review_env):
    env = review_env
    experiment = new_experiment(env)
    before = deepcopy(env.admin.tables)
    calls = len(env.agent.calls)
    for url in (card_url(), plan_url(experiment)):
        denied = env.client.post(
            url + "/delegate", headers=BOB_AUTH, json={"base_version": 1, "consent": True},
        )
        assert denied.status_code == 404
    assert env.admin.tables == before
    assert len(env.agent.calls) == calls


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_delegation_provider_failure_never_authorizes_or_approves(review_env, failure):
    env = review_env
    experiment = new_experiment(env)
    before = deepcopy(env.admin.tables)
    env.agent.failure = failure
    response = delegate_plan(env, experiment)
    assert response.status_code == 502
    assert PROVIDER_SECRET not in response.text
    assert env.admin.tables == before


def test_delegation_rejects_partial_recommendations_without_writes(review_env):
    env = review_env
    experiment = new_experiment(env)
    before = deepcopy(env.admin.tables)
    env.agent.corrupt = lambda payload: {
        **payload, "recommendations": payload["recommendations"][:-1],
    }
    assert delegate_plan(env, experiment).status_code == 502
    assert env.admin.tables == before


def test_delegation_rechecks_version_after_slow_recommendation(review_env):
    env = review_env
    experiment = new_experiment(env)

    async def insert_competing_version():
        row = deepcopy(env.admin.tables["experiment_plans"][0])
        row.update(id=str(uuid4()), version=2)
        env.admin.tables["experiment_plans"].append(row)

    env.agent.on_review = insert_competing_version
    response = delegate_plan(env, experiment)
    assert response.status_code == 409
    assert len(env.admin.tables["experiment_plans"]) == 2
    assert all(row["approved_at"] is None for row in env.admin.tables["experiment_plans"])


def test_delegated_plan_cannot_silently_be_relabelled_expert(review_env):
    env = review_env
    experiment = new_experiment(env)
    delegated = ok(delegate_plan(env, experiment), 201)
    before = deepcopy(env.admin.tables)
    response = env.client.post(
        plan_url(experiment, 2) + "/approve", headers=AUTH, json={"review_mode": "expert"},
    )
    assert response.status_code == 409
    assert env.admin.tables == before
    assert ok(env.client.get(plan_url(experiment, 2), headers=AUTH)) == delegated


def test_manual_edit_revokes_delegation_in_new_plan_only(review_env):
    env = review_env
    experiment = new_experiment(env)
    delegated = ok(delegate_plan(env, experiment), 201)
    changed = ok(env.client.post(
        f"/v1/experiments/{experiment['id']}/plans", headers=AUTH,
        json={"base_version": 2, "change_reason": "manual adjustment", "review_mode": "expert",
              "changes": [{"field_path": WEIGHT_DECAY, "value": 0.0}]},
    ), 201)
    assert changed["plan"]["review_mode"] == "expert"
    assert changed["plan"]["delegated_at"] is None
    assert changed["plan"]["delegated_by"] is None
    assert changed["approved_at"] is None
    assert env.client.post(plan_url(experiment, 3) + "/approve", headers=AUTH).status_code == 409
    assert ok(env.client.get(plan_url(experiment, 2), headers=AUTH)) == delegated


def test_card_delegation_preserves_missing_evidence_and_audits_authorization(review_env):
    env = review_env
    old = ok(env.client.get(card_url(), headers=AUTH))
    delegated = ok(env.client.post(
        card_url() + "/delegate", headers=AUTH, json={"base_version": 1, "consent": True},
    ), 201)
    card = delegated["card"]
    assert card["review_mode"] == "delegated"
    assert card["delegated_by"] == str(USER_ID)
    assert card["optimizer"]["status"] == "inferred"
    assert card["optimizer"]["confirmed_by"] == "auto"
    assert card["optimizer"]["human_confirmed"] is False
    assert card["loss_function"]["status"] == "missing"
    assert card["loss_function"]["value"] is None
    assert card["loss_function"]["source_page"] is None
    assert card["loss_function"]["human_confirmed"] is False
    assert card["review_decisions"]
    assert all(d["actor"] == str(USER_ID) and d["id"] for d in card["review_decisions"])
    assert ok(env.client.get(card_url() + "?version=1", headers=AUTH)) == old
    assert env.agent.calls == []
    assert env.client.post(
        card_url() + "/delegate", headers=AUTH, json={"base_version": 1, "consent": True},
    ).status_code == 409
    experiment = new_experiment(env)
    assert experiment["status"] == "approved"
    assert experiment["plan"]["approved_at"]
    assert experiment["plan"]["plan"]["delegated_by"] == str(USER_ID)
    assert experiment["plan"]["plan"]["training_config"]["loss_function"]["confirmed_by"] == "auto"


def test_card_manual_confirm_revokes_authorization_with_trace(review_env):
    env = review_env
    delegated = ok(env.client.post(
        card_url() + "/delegate", headers=AUTH, json={"base_version": 1, "consent": True},
    ), 201)
    confirmed = ok(env.client.post(card_url() + "/confirm", headers=AUTH), 201)
    assert confirmed["card"]["review_mode"] == "assisted"
    assert confirmed["card"]["delegated_by"] is None
    assert confirmed["card"]["delegated_at"] is None
    assert {e["field_path"] for e in confirmed["card"]["review_decisions"]} == {
        "card.status", "review_mode",
    }
    assert ok(env.client.get(card_url() + "?version=2", headers=AUTH)) == delegated
