"""API input-gate tests: unsupported papers never reach the Runner.

Uses the recording runner transport to assert zero container/run dispatches for
scanned PDF, CIFAR-10, and non-LeNet cards.
"""

import json
from uuid import uuid4

import httpx
import pytest
from reproagent_api.clients.agent import AgentClient
from reproagent_api.clients.runner import RunnerClient
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import (
    WorkflowContext,
    initial_state,
    make_memory_checkpointer,
    start_workflow,
)
from reproagent_schemas import (
    BudgetConfig,
    LLMUsage,
    ParsedDocument,
    ReproductionCard,
    TextBlock,
    WorkflowStatus,
)
from test_runs import (
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    agent_transport,
    make_card,
)

PAPER_ID = uuid4()
WORKFLOW_ID = uuid4()
THREAD_ID = str(WORKFLOW_ID)


def clean_document() -> ParsedDocument:
    return ParsedDocument(
        num_pages=2,
        blocks=[
            TextBlock(id="p1-b1", text="LeNet-5 MNIST handwritten digits", page=1),
            TextBlock(id="p2-b1", text="Convolutional architecture", page=2),
        ],
        ocr_required_pages=[],
    )


def scanned_document() -> ParsedDocument:
    return ParsedDocument(
        num_pages=5,
        blocks=[],
        ocr_required_pages=[1, 2, 3],
    )


def cifar_card() -> ReproductionCard:
    return make_card().model_copy(
        update={"dataset": make_card().dataset.model_copy(update={"value": "CIFAR-10"})}
    )


def resnet_card() -> ReproductionCard:
    from reproagent_schemas import EvidenceField, EvidenceStatus

    return make_card().model_copy(
        update={
            "model_architecture": EvidenceField(
                value="ResNet-50",
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...ResNet-50...",
                source_page=3,
            ),
            "method": EvidenceField(
                value="residual learning",
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...residual learning...",
                source_page=1,
            ),
        }
    )


def _seed() -> dict:
    return {
        "projects": [{"id": str(PROJECT_ID), "user_id": str(USER_ID)}],
        "papers": [
            {
                "id": str(PAPER_ID),
                "project_id": str(PROJECT_ID),
                "title": "Test",
                "pdf_path": "u/p.pdf",
                "status": "uploaded",
                "projects": {"user_id": str(USER_ID)},
            }
        ],
        "workflows": [
            {
                "id": str(WORKFLOW_ID),
                "thread_id": THREAD_ID,
                "project_id": str(PROJECT_ID),
                "paper_id": str(PAPER_ID),
                "status": "uploaded",
                "budget_json": {},
            }
        ],
        "reproduction_specs": [],
        "experiments": [],
        "experiment_plans": [],
        "execution_runs": [],
        "generated_files": [],
        "execution_run_files": [],
        "execution_artifacts": [],
        "agent_traces": [],
        "reproduction_reports": [],
    }


class RecordingRunner:
    """Runner client stub that records every HTTP call for assertions."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, request.url.path))
            return httpx.Response(404, json={"detail": "should never be reached"})

        return httpx.MockTransport(handler)

    @property
    def dispatch_count(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.startswith("/runs"))


def _gate_agent_transport(
    doc: ParsedDocument,
    card: ReproductionCard,
    *,
    support_check_endpoint: bool = True,
) -> httpx.MockTransport:
    """Agent transport stub: supports parse/extract/ground/support-check."""
    usage = LLMUsage(
        model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/parse":
            return httpx.Response(
                200, json={"document": doc.model_dump(mode="json")}
            )
        if path == "/v1/extract":
            return httpx.Response(
                200,
                json={
                    "card_raw": card.model_dump(mode="json"),
                    "usages": [usage.model_dump(mode="json")],
                },
            )
        if path == "/v1/papers/support-check":
            from reproagent_agent.planning import check_supported
            from reproagent_schemas import ReproductionCard as RC

            body = json.loads(request.content)
            parsed_doc = ParsedDocument.model_validate(body["document"])
            parsed_card = RC.model_validate(body["card"]) if body.get("card") is not None else None
            result = check_supported(parsed_doc, parsed_card)
            return httpx.Response(
                200,
                json={
                    "supported": result.supported,
                    "code": result.code,
                    "message": result.message,
                },
            )
        if path == "/v1/ground":
            return httpx.Response(200, json={"card": card.model_dump(mode="json")})
        # fall through to the codegen/evaluate transport for supported flows
        return agent_transport().handle_request(request)

    return httpx.MockTransport(handler)


def _ctx(admin, results: list, doc: ParsedDocument, card: ReproductionCard):
    settings = ApiSettings(
        supabase_anon_key="anon-test",
        supabase_service_role_key="service-test",
        _env_file=None,
    )
    admin.storage.files["u/p.pdf"] = b"%PDF-1.4 fake"
    recorder = RecordingRunner()
    ctx = WorkflowContext(
        admin=admin,
        agent=AgentClient(settings, transport=_gate_agent_transport(doc, card)),
        runner=RunnerClient(settings, transport=recorder.transport()),
        settings=settings,
        budget_config=BudgetConfig(),
    )
    ctx.recorder = recorder  # type: ignore
    return ctx


def _state(admin) -> dict:
    return initial_state(admin.tables["workflows"][0], BudgetConfig())


@pytest.mark.asyncio
async def test_scanned_pdf_fails_before_extractor():
    admin = FakeAdmin(_seed(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(admin, [], scanned_document(), make_card())
    try:
        await start_workflow(ctx, make_memory_checkpointer(), _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.FAILED.value
    assert "SCANNED_PDF" in admin.tables["workflows"][0]["error"]
    assert ctx.recorder.dispatch_count == 0  # type: ignore
    assert admin.tables["reproduction_specs"] == []


@pytest.mark.asyncio
async def test_non_mnist_dataset_fails_before_planning_or_runner():
    admin = FakeAdmin(_seed(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(admin, [], clean_document(), cifar_card())
    try:
        await start_workflow(ctx, make_memory_checkpointer(), _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.FAILED.value
    assert "UNSUPPORTED_DATASET" in admin.tables["workflows"][0]["error"]
    assert ctx.recorder.dispatch_count == 0  # type: ignore
    assert admin.tables["experiments"] == []
    assert admin.tables["execution_runs"] == []


@pytest.mark.asyncio
async def test_non_lenet_model_fails_before_planning_or_runner():
    admin = FakeAdmin(_seed(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(admin, [], clean_document(), resnet_card())
    try:
        await start_workflow(ctx, make_memory_checkpointer(), _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.FAILED.value
    assert "UNSUPPORTED_MODEL" in admin.tables["workflows"][0]["error"]
    assert ctx.recorder.dispatch_count == 0  # type: ignore
    assert admin.tables["experiments"] == []


@pytest.mark.asyncio
async def test_supported_lenet_mnist_proceeds_past_gate():
    """A valid LeNet-5 + MNIST paper must pass the gate and reach human_review."""
    admin = FakeAdmin(_seed(), {str(PROJECT_ID): str(USER_ID)})
    ctx = _ctx(admin, [], clean_document(), make_card())
    try:
        await start_workflow(ctx, make_memory_checkpointer(), _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    # Workflow paused at human_review gate (not failed by support gate)
    assert admin.tables["workflows"][0]["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value
    assert admin.tables["workflows"][0].get("error") is None
    assert ctx.recorder.dispatch_count == 0  # type: ignore  # gate doesn't block, just hasn't reached runner yet
