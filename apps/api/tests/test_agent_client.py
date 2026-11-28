from uuid import uuid4

import httpx
from reproagent_api.clients.agent import AgentClient
from reproagent_api.config import ApiSettings
from reproagent_schemas import (
    EvidenceField,
    EvidenceStatus,
    ParsedDocument,
    ReproductionCard,
    UnderstandRequest,
    UnderstandResponse,
)


async def test_agent_client_understand_contract():
    paper_id = uuid4()
    field = EvidenceField(
        value="LeNet-5",
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote="We present LeNet-5.",
        source_page=1,
    )
    expected = UnderstandResponse(
        paper_id=paper_id,
        document=ParsedDocument(num_pages=1),
        card=ReproductionCard(
            method=field,
            model_architecture=field,
            dataset=field,
            optimizer=field,
            loss_function=field,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/understand"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, json=expected.model_dump(mode="json"))

    settings = ApiSettings(agent_base_url="http://agent.test", _env_file=None)
    client = AgentClient(settings, transport=httpx.MockTransport(handler))
    request = UnderstandRequest(paper_id=paper_id, pdf_b64="aGVsbG8=")
    response = await client.understand(request)
    assert response.paper_id == paper_id
    assert response.card.method.source_page == 1
    await client.aclose()
