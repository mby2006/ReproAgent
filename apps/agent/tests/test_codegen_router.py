from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import LLMAdapter
from reproagent_agent.main import create_app


class NullLLM(LLMAdapter):
    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        raise AssertionError("codegen must not call the LLM")


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


def get_plan(client: TestClient) -> dict:
    response = client.post("/v1/plan", json={"card": CARD})
    assert response.status_code == 200
    return response.json()["plan"]


def test_codegen_returns_bundle():
    with make_client() as client:
        plan = get_plan(client)
        response = client.post("/v1/codegen", json={"card": CARD, "plan": plan})
    assert response.status_code == 200
    bundle = response.json()["bundle"]
    assert bundle["entrypoint"] == "train.py"
    assert bundle["code_hash"]
    assert {f["path"] for f in bundle["files"]} >= {"train.py", "model.py", "config.yaml"}


def test_validate_accepts_generated_bundle():
    with make_client() as client:
        plan = get_plan(client)
        bundle = client.post("/v1/codegen", json={"card": CARD, "plan": plan}).json()["bundle"]
        response = client.post("/v1/codegen/validate", json={"bundle": bundle})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_validate_rejects_banned_code():
    with make_client() as client:
        plan = get_plan(client)
        bundle = client.post("/v1/codegen", json={"card": CARD, "plan": plan}).json()["bundle"]
        for file in bundle["files"]:
            if file["path"] == "model.py":
                file["content"] = "import os\n"
        response = client.post("/v1/codegen/validate", json={"bundle": bundle})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(i["code"] == "banned_import" for i in body["issues"])


def test_classify_endpoint():
    with make_client() as client:
        response = client.post(
            "/v1/codegen/classify",
            json={"stderr": "ModuleNotFoundError: No module named 'x'", "exit_code": 1},
        )
    assert response.status_code == 200
    assert response.json()["category"] == "IMPORT_ERROR"


def test_repair_oom_flags_replan():
    with make_client() as client:
        plan = get_plan(client)
        bundle = client.post("/v1/codegen", json={"card": CARD, "plan": plan}).json()["bundle"]
        response = client.post(
            "/v1/codegen/repair",
            json={
                "bundle": bundle,
                "classification": {
                    "category": "OUT_OF_MEMORY",
                    "reason": "killed",
                    "repairable": True,
                },
                "plan": plan,
            },
        )
    assert response.status_code == 200
    assert response.json()["replan_required"] is True