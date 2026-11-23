"""HTTP contract of the agent comparison endpoints (no LLM involved)."""

from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import LLMAdapter
from reproagent_agent.main import create_app


class NullLLM(LLMAdapter):
    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        raise AssertionError("comparison endpoints must not call the LLM")


def make_client() -> TestClient:
    return TestClient(create_app(AgentSettings(_env_file=None), llm=NullLLM()))


def explicit(value, page=8):
    return {
        "value": value,
        "status": "paper_explicit",
        "evidence_quote": f"...{value}...",
        "source_page": page,
    }


def card(dataset="MNIST handwritten digits", learning_rate="0.01"):
    hyperparameters = {"learning_rate": explicit(learning_rate)} if learning_rate else {}
    return {
        "method": explicit("LeNet-5 gradient-based learning"),
        "model_architecture": explicit("LeNet-5 convolutional network"),
        "dataset": explicit(dataset),
        "optimizer": explicit("SGD"),
        "loss_function": explicit("cross entropy"),
        "hyperparameters": hyperparameters,
        "evaluation_metrics": [explicit("accuracy")],
    }


def reference(label, optimizer, value):
    return {
        "label": label,
        "quote": f"{optimizer} {value}",
        "source_page": 7,
        "table_id": "t3",
        "metric": "accuracy",
        "value": value,
    }


def plan_body(**overrides):
    body = {
        "card": card(learning_rate=None),
        "question": "在相同条件下，Adam 是否比 SGD 准确率更高？",
        "group_specs": [
            {
                "key": "a",
                "label": "SGD 组",
                "optimizer": "sgd",
                "learning_rate": 0.01,
                "references": [reference("表3 · SGD", "SGD", 0.905)],
            },
            {
                "key": "b",
                "label": "Adam 组",
                "optimizer": "adam",
                "learning_rate": 0.001,
                "references": [reference("表3 · Adam", "Adam", 0.932)],
            },
        ],
    }
    body.update(overrides)
    return body


def test_plan_endpoint_returns_pending_items():
    with make_client() as client:
        response = client.post("/v1/comparisons/plan", json=plan_body())
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["support"]["ok"] is True
    assert plan["seeds"] == [42]
    assert [group["optimizer"]["value"] for group in plan["groups"]] == ["sgd", "adam"]


def test_plan_endpoint_names_missing_capability():
    with make_client() as client:
        response = client.post(
            "/v1/comparisons/plan", json=plan_body(card=card(dataset="CIFAR-10 images"))
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert any("CIFAR-10" in gap for gap in detail["missing"])


def test_verify_endpoint_computes_delta_from_counts():
    with make_client() as client:
        plan = client.post("/v1/comparisons/plan", json=plan_body()).json()["plan"]
        from uuid import uuid4

        def observation(key, optimizer, correct):
            return {
                "run_id": str(uuid4()),
                "group_key": key,
                "seed": 42,
                "status": "succeeded",
                "correct_count": correct,
                "evaluated_count": 1024,
                "metric_value": 0.5,
                "fingerprint": {
                    "dataset_split_sha256": "split",
                    "init_weights_sha256": "init",
                    "sample_order_sha256": "order",
                    "template_sha256": "template",
                    "seed": 42,
                    "batch_size": 64,
                    "epochs": 2,
                    "train_subset_size": 4096,
                    "test_subset_size": 1024,
                    "loss_function": "cross_entropy",
                    "optimizer": optimizer,
                    "learning_rate": 0.01,
                    "code_hash": "code",
                    "image_digest": "img",
                },
            }

        response = client.post(
            "/v1/comparisons/verify",
            json={
                "plan": plan,
                "observations": [observation("a", "sgd", 880), observation("b", "adam", 921)],
                "requested_pairs": 1,
            },
        )
    assert response.status_code == 200, response.text
    outcome = response.json()["outcome"]
    assert outcome["status"] == "completed"
    assert outcome["mean_a"] == 880 / 1024
    assert outcome["mean_b"] == 921 / 1024
    assert round(outcome["mean_delta_percentage_points"], 4) == round((880 - 921) / 1024 * 100, 4)


def test_report_endpoint_emits_markdown():
    with make_client() as client:
        plan = client.post("/v1/comparisons/plan", json=plan_body()).json()["plan"]
        from uuid import uuid4

        references = {
            "comparison_id": str(uuid4()),
            "plan_id": str(uuid4()),
            "plan_version_id": str(uuid4()),
            "plan_version": 1,
            "paper_id": str(uuid4()),
            "spec_version_id": str(uuid4()),
            "spec_version": 1,
            "run_ids": [],
            "code_hashes": [],
        }
        outcome = {
            "status": "incomplete",
            "target_metric": "accuracy",
            "seeds": [],
            "completed_pairs": 0,
            "requested_pairs": 1,
            "blockers": ["A 组没有运行记录"],
            "verifications": [],
        }
        response = client.post(
            "/v1/comparisons/report",
            json={"references": references, "plan": plan, "outcome": outcome},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "对照未完成" in body["markdown"]
    assert "阻塞" in body["markdown"]


def test_plan_endpoint_maps_invalid_input_to_422():
    body = plan_body(seeds=[1, 2, 3, 4])
    with make_client() as client:
        response = client.post("/v1/comparisons/plan", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "对照计划参数无效"
    assert any("seeds" in gap for gap in detail["missing"])
