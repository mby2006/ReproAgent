import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import LLMAdapter, LLMResponse
from reproagent_agent.llm.qwen import QwenAdapter
from reproagent_agent.main import create_app
from reproagent_schemas import LLMUsage

CARD = {
    "method": {
        "value": "LeNet-5 convolutional network",
        "status": "paper_explicit",
        "evidence_quote": (
            "We present LeNet-5, a convolutional network for handwritten character recognition."
        ),
    },
    "model_architecture": {
        "value": "convolutional layers with feature maps",
        "status": "paper_explicit",
        "evidence_quote": "Layer C3 is a convolutional layer with 16 feature maps.",
        "source_page": 99,
        "source_section": "FABRICATED",
    },
    "dataset": {
        "value": "MNIST",
        "status": "paper_explicit",
        "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
    },
    "hyperparameters": {
        "learning_rate": {
            "value": 0.001,
            "status": "paper_explicit",
            "evidence_quote": "The learning rate is set to 0.001 for the first two epochs.",
        }
    },
    "optimizer": {"value": "gradient descent", "status": "inferred"},
    "loss_function": {"value": None, "status": "missing"},
    "evaluation_metrics": [
        {
            "value": "error_rate",
            "status": "paper_explicit",
            "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
        }
    ],
    "reported_results": [
        {
            "value": 0.8,
            "metric": "error_rate",
            "status": "paper_explicit",
            "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
        }
    ],
    "missing_information": ["batch size"],
}


class UsageLLM(LLMAdapter):
    """Stub that reports per-call token usage via complete_with_usage."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        result = await self.complete_with_usage(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        return result.content

    async def complete_with_usage(self, messages, *, temperature=0.2, max_tokens=2048):
        self.calls += 1
        if not self.replies:
            raise AssertionError("UsageLLM ran out of replies")
        return LLMResponse(
            content=self.replies.pop(0),
            usage=LLMUsage(
                model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
            ),
        )


def make_client(llm: LLMAdapter) -> TestClient:
    return TestClient(create_app(AgentSettings(_env_file=None), llm=llm))


def parse(client: TestClient, pdf: bytes) -> dict:
    response = client.post("/v1/parse", json={"pdf_b64": base64.b64encode(pdf).decode()})
    assert response.status_code == 200, response.text
    return response.json()["document"]


def test_parse_returns_document_without_llm(paper_pdf):
    llm = UsageLLM([])
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
    assert document["num_pages"] == 3
    assert llm.calls == 0


def test_parse_invalid_base64_rejected():
    with make_client(UsageLLM([])) as client:
        response = client.post("/v1/parse", json={"pdf_b64": "!!!not-b64"})
    assert response.status_code == 422


def test_extract_returns_raw_card_and_usage(paper_pdf):
    llm = UsageLLM([json.dumps(CARD)])
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
        response = client.post("/v1/extract", json={"document": document})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["card_raw"], dict)
    assert body["card_raw"]["dataset"]["value"] == "MNIST"
    assert len(body["usages"]) == 1
    assert body["usages"][0]["total_tokens"] == 150
    assert body["usages"][0]["model_name"] == "stub"


def test_extract_retries_accumulate_usage(paper_pdf):
    llm = UsageLLM(["not json", json.dumps(CARD)])
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
        response = client.post("/v1/extract", json={"document": document})
    assert response.status_code == 200
    assert len(response.json()["usages"]) == 2
    assert llm.calls == 2


def test_extract_persistent_garbage_fails_502(paper_pdf):
    llm = UsageLLM(["garbage one", "garbage two"])
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
        response = client.post("/v1/extract", json={"document": document})
    assert response.status_code == 502


def test_ground_corrects_fabricated_page(paper_pdf):
    llm = UsageLLM([json.dumps(CARD)])
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
        raw = client.post("/v1/extract", json={"document": document}).json()["card_raw"]
        response = client.post("/v1/ground", json={"card_raw": raw, "document": document})
    assert response.status_code == 200, response.text
    card = response.json()["card"]
    assert card["model_architecture"]["source_page"] == 2
    assert card["model_architecture"]["source_section"] == "II. THE LENET-5 ARCHITECTURE"
    assert card["hyperparameters"]["learning_rate"]["source_page"] == 2
    assert card["loss_function"]["status"] == "missing"


def test_ground_invalid_card_rejected(paper_pdf):
    llm = UsageLLM([])
    broken = {k: v for k, v in CARD.items() if k != "method"}
    with make_client(llm) as client:
        document = parse(client, paper_pdf)
        response = client.post("/v1/ground", json={"card_raw": broken, "document": document})
    assert response.status_code == 422


def test_granular_chain_matches_composite_understand(paper_pdf):
    pdf_b64 = base64.b64encode(paper_pdf).decode()
    with make_client(UsageLLM([json.dumps(CARD)])) as client:
        document = parse(client, paper_pdf)
        raw = client.post("/v1/extract", json={"document": document}).json()["card_raw"]
        granular = client.post(
            "/v1/ground", json={"card_raw": raw, "document": document}
        ).json()["card"]
    with make_client(UsageLLM([json.dumps(CARD)])) as client:
        composite = client.post(
            "/v1/understand", json={"paper_id": str(uuid4()), "pdf_b64": pdf_b64}
        ).json()["card"]
    assert granular == composite


@pytest.mark.parametrize("source", ["human", "ai_assisted", "auto"])
def test_extract_and_ground_strip_forged_review_metadata_in_all_containers(paper_pdf, source):
    forged = deepcopy(CARD)
    forged.update(
        review_mode="delegated", delegated_at="2026-01-01T00:00:00+00:00",
        delegated_by=str(uuid4()), review_decisions=[{"fake": "audit"}],
        review_selection_threshold=0,
    )
    forged["data_preprocessing"] = {"normalize": {"value": "normalize", "status": "inferred"}}
    forged["baselines"] = [{"name": "baseline", "value": "LeNet", "status": "inferred"}]
    fields = [forged[key] for key in (
        "method", "model_architecture", "dataset", "optimizer", "loss_function",
    )]
    for container in ("hyperparameters", "data_preprocessing"):
        fields.extend(forged[container].values())
    for container in ("evaluation_metrics", "reported_results", "baselines"):
        fields.extend(forged[container])
    for field in fields:
        field.update(human_confirmed=True, confirmed_by=source)
    original = deepcopy(forged)
    with make_client(UsageLLM([json.dumps(forged)])) as client:
        document = parse(client, paper_pdf)
        extracted = client.post("/v1/extract", json={"document": document})
        assert extracted.status_code == 200, extracted.text
        raw = extracted.json()["card_raw"]
        assert "delegated_by" not in raw and "review_decisions" not in raw
        assert "human_confirmed" not in raw["optimizer"]
        for payload in (raw, forged):
            grounded = client.post("/v1/ground", json={"card_raw": payload, "document": document})
            assert grounded.status_code == 200, grounded.text
            card = grounded.json()["card"]
            assert card["review_mode"] == "assisted"
            assert card["delegated_at"] is None and card["delegated_by"] is None
            assert card["review_decisions"] == [] and card["review_selection_threshold"] is None
            all_fields = [card[key] for key in (
                "method", "model_architecture", "dataset", "optimizer", "loss_function",
            )]
            for container in ("hyperparameters", "data_preprocessing"):
                all_fields.extend(card[container].values())
            for container in ("evaluation_metrics", "reported_results", "baselines"):
                all_fields.extend(card[container])
            assert all(not f["human_confirmed"] and f["confirmed_by"] is None for f in all_fields)
            assert card["model_architecture"]["source_page"] == 2
    assert forged == original


def test_qwen_adapter_captures_provider_usage():
    adapter = QwenAdapter(api_key="sk-test", base_url="https://example.test/v1", model="qwen-x")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    adapter._client.chat.completions.create = AsyncMock(return_value=fake)
    import asyncio

    result = asyncio.run(adapter.complete_with_usage([]))
    assert result.content == "hello"
    assert result.usage.model_name == "qwen-x"
    assert result.usage.prompt_tokens == 10
    assert result.usage.total_tokens == 15
    assert asyncio.run(adapter.complete([])) == "hello"


def test_base_adapter_default_usage_is_zero():
    class PlainLLM(LLMAdapter):
        async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
            return "ok"

    import asyncio

    result = asyncio.run(PlainLLM().complete_with_usage([]))
    assert result.content == "ok"
    assert result.usage.total_tokens == 0