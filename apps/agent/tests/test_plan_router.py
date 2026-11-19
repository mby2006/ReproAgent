from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import LLMAdapter
from reproagent_agent.main import create_app


class NullLLM(LLMAdapter):
    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        raise AssertionError("planning must not call the LLM")


def make_client() -> TestClient:
    return TestClient(create_app(AgentSettings(_env_file=None), llm=NullLLM()))


def explicit(value, page=8):
    return {
        "value": value,
        "status": "paper_explicit",
        "evidence_quote": f"...{value}...",
        "source_page": page,
    }


CARD = {
    "method": explicit("LeNet-5 gradient-based learning"),
    "model_architecture": explicit("LeNet-5 convolutional network"),
    "dataset": explicit("MNIST handwritten digits"),
    "optimizer": explicit("SGD"),
    "loss_function": explicit("cross-entropy"),
    "hyperparameters": {"learning_rate": explicit("0.001")},
    "evaluation_metrics": [explicit("accuracy")],
}


def test_plan_returns_plan_with_model_config_key():
    with make_client() as client:
        response = client.post("/v1/plan", json={"card": CARD})
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["model_config"]["architecture"] == "lenet-5"
    assert plan["training_config"]["batch_size"]["source"] == "mvp_scaling"
    assert plan["scaling_profile"] == "mvp_lenet5_mnist_v1"


def test_plan_applies_scaling_overrides():
    with make_client() as client:
        response = client.post(
            "/v1/plan", json={"card": CARD, "epochs": 5, "batch_size": 32, "max_seconds": 300}
        )
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["training_config"]["epochs"]["value"] == 5
    assert plan["training_config"]["batch_size"]["value"] == 32
    assert plan["runtime_budget"]["max_seconds"] == 300


def test_plan_rejects_unsupported_paper():
    bad = {**CARD, "dataset": explicit("CIFAR-10 images")}
    with make_client() as client:
        response = client.post("/v1/plan", json={"card": bad})
    assert response.status_code == 422


def test_plan_rejects_invalid_card():
    with make_client() as client:
        response = client.post("/v1/plan", json={"card": {"dataset": explicit("MNIST")}})
    assert response.status_code == 422
