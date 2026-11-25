import base64
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import ChatMessage, LLMAdapter
from reproagent_agent.main import create_app

VALID_CARD = {
    "method": {
        "value": "LeNet-5 convolutional network",
        "status": "paper_explicit",
        "evidence_quote": (
            "We present LeNet-5, a convolutional network for handwritten character recognition."
        ),
        "confidence": 0.95,
        "human_confirmed": False,
    },
    "model_architecture": {
        "value": "convolutional layers with feature maps",
        "status": "paper_explicit",
        "evidence_quote": "Layer C3 is a convolutional layer with 16 feature maps.",
        "source_page": 99,
        "source_section": "FABRICATED",
        "confidence": 0.9,
    },
    "dataset": {
        "value": "MNIST",
        "status": "paper_explicit",
        "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
        "confidence": 0.9,
    },
    "data_preprocessing": {},
    "hyperparameters": {
        "learning_rate": {
            "value": 0.001,
            "status": "paper_explicit",
            "evidence_quote": "The learning rate is set to 0.001 for the first two epochs.",
            "confidence": 0.95,
        }
    },
    "optimizer": {"value": "gradient descent", "status": "inferred", "confidence": 0.5},
    "loss_function": {"value": None, "status": "missing"},
    "evaluation_metrics": [
        {
            "value": "error_rate",
            "status": "paper_explicit",
            "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
            "confidence": 0.9,
        }
    ],
    "reported_results": [
        {
            "value": 0.8,
            "metric": "error_rate",
            "condition": "MNIST test set",
            "status": "paper_explicit",
            "evidence_quote": "The error rate on the MNIST test set is 0.8 percent.",
            "confidence": 0.95,
        }
    ],
    "baselines": [],
    "missing_information": ["batch size"],
}


class MockLLM(LLMAdapter):
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("MockLLM ran out of replies")
        return self.replies.pop(0)


def make_client(llm: LLMAdapter) -> TestClient:
    return TestClient(create_app(AgentSettings(_env_file=None), llm=llm))


def post_pdf(client: TestClient, pdf: bytes):
    return client.post(
        "/v1/understand",
        json={"paper_id": str(uuid4()), "pdf_b64": base64.b64encode(pdf).decode()},
    )


class TestUnderstandEndpoint:
    def test_happy_path_grounds_pages(self, paper_pdf):
        llm = MockLLM([json.dumps(VALID_CARD)])
        with make_client(llm) as client:
            response = post_pdf(client, paper_pdf)
        assert response.status_code == 200
        body = response.json()
        card = body["card"]
        assert body["document"]["num_pages"] == 3
        assert card["hyperparameters"]["learning_rate"]["source_page"] == 2
        assert card["reported_results"][0]["source_page"] == 3
        assert card["loss_function"]["status"] == "missing"
        assert card["optimizer"]["status"] == "inferred"

    def test_fabricated_page_is_overridden(self, paper_pdf):
        llm = MockLLM([json.dumps(VALID_CARD)])
        with make_client(llm) as client:
            card = post_pdf(client, paper_pdf).json()["card"]
        arch = card["model_architecture"]
        assert arch["source_page"] == 2
        assert arch["source_section"] == "II. THE LENET-5 ARCHITECTURE"

    def test_invalid_json_then_valid_retries(self, paper_pdf):
        llm = MockLLM(["this is not json", json.dumps(VALID_CARD)])
        with make_client(llm) as client:
            response = post_pdf(client, paper_pdf)
        assert response.status_code == 200
        assert len(llm.calls) == 2

    def test_schema_failure_then_valid_retries(self, paper_pdf):
        broken = {k: v for k, v in VALID_CARD.items() if k != "method"}
        llm = MockLLM([json.dumps(broken), json.dumps(VALID_CARD)])
        with make_client(llm) as client:
            response = post_pdf(client, paper_pdf)
        assert response.status_code == 200
        assert len(llm.calls) == 2

    def test_persistent_garbage_fails_with_502(self, paper_pdf):
        llm = MockLLM(["garbage one", "garbage two"])
        with make_client(llm) as client:
            response = post_pdf(client, paper_pdf)
        assert response.status_code == 502

    def test_invalid_base64_rejected(self):
        llm = MockLLM([])
        with make_client(llm) as client:
            response = client.post(
                "/v1/understand", json={"paper_id": str(uuid4()), "pdf_b64": "!!!not-b64"}
            )
        assert response.status_code == 422

    def test_non_pdf_bytes_rejected(self):
        llm = MockLLM([])
        with make_client(llm) as client:
            response = post_pdf(client, b"hello, not a pdf")
        assert response.status_code == 422

    def test_oversized_decoded_pdf_rejected_before_graph(self, monkeypatch):
        from fastapi import HTTPException
        from reproagent_agent.routers import understand

        monkeypatch.setattr(understand, "MAX_PDF_BYTES", 4)
        monkeypatch.setattr(understand, "MAX_PDF_BASE64_LENGTH", 8)
        with pytest.raises(HTTPException) as exc:
            understand._decode_pdf(base64.b64encode(b"%PDF-1.4 oversized").decode())
        assert exc.value.status_code == 413

    def test_code_fenced_json_accepted(self, paper_pdf):
        llm = MockLLM(["```json\n" + json.dumps(VALID_CARD) + "\n```"])
        with make_client(llm) as client:
            response = post_pdf(client, paper_pdf)
        assert response.status_code == 200
