"""Unit tests for the 15-node workflow graph (MemorySaver + stub transports).

Exercises orchestration mechanics -- human interrupts + checkpoint resume, the
repair/replan loop, terminal-always-evaluates routing, budget ceilings, and
credential-redacted node traces -- without Docker, an LLM, or a real database.
Real PDF parse / grounding / MNIST execution are covered by the integration E2E.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from reproagent_agent.planning import build_plan
from reproagent_api.clients.agent import AgentClient
from reproagent_api.clients.runner import RunnerClient
from reproagent_api.config import ApiSettings
from reproagent_api.workflow import (
    BudgetExceeded,
    WorkflowContext,
    initial_state,
    make_memory_checkpointer,
    redact,
    resume_workflow,
    route_after_classify,
    route_after_execute,
    route_after_validate,
    start_workflow,
)
from reproagent_schemas import (
    BudgetConfig,
    BudgetState,
    ErrorCategory,
    ErrorClassification,
    ExperimentPlan,
    FieldRecommendation,
    LLMUsage,
    ParsedDocument,
    RecommendResponse,
    ReproductionCard,
    ReviewMode,
    ReviewRequest,
    RunStatus,
    TextBlock,
    WorkflowStatus,
)
from test_runs import (
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    agent_transport,
    make_card,
    make_result,
    runner_transport,
)

PAPER_ID = uuid4()
WORKFLOW_ID = uuid4()
THREAD_ID = str(WORKFLOW_ID)

ALL_NODES = {
    "ingest_paper",
    "extract_paper",
    "build_reproduction_spec",
    "validate_evidence",
    "human_review",
    "plan_experiment",
    "human_approve_plan",
    "generate_code",
    "validate_code",
    "execute_code",
    "classify_error",
    "repair_code",
    "evaluate_result",
    "generate_report",
    "persist_results",
}


def _document() -> ParsedDocument:
    return ParsedDocument(
        title="LeNet-5", num_pages=1, blocks=[TextBlock(id="p1-b1", text="LeNet-5 MNIST", page=1)]
    )


def workflow_agent_transport(
    validate_ok: bool = True, validate_issues: list[dict] | None = None
) -> httpx.MockTransport:
    base = agent_transport()
    card = make_card()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/parse":
            return httpx.Response(200, json={"document": _document().model_dump(mode="json")})
        if path == "/v1/extract":
            usage = LLMUsage(
                model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
            )
            return httpx.Response(
                200,
                json={
                    "card_raw": card.model_dump(mode="json"),
                    "usages": [usage.model_dump(mode="json")],
                },
            )
        if path == "/v1/ground":
            grounded = ReproductionCard.model_validate(body["card_raw"])
            return httpx.Response(200, json={"card": grounded.model_dump(mode="json")})
        if path == "/v1/plan":
            plan = build_plan(ReproductionCard.model_validate(body["card"]))
            return httpx.Response(200, json={"plan": plan.model_dump(mode="json")})
        if path == "/v1/codegen/validate" and not validate_ok:
            issues = validate_issues or [{"code": "banned", "message": "uses os.environ"}]
            return httpx.Response(200, json={"ok": False, "issues": issues})
        return base.handle_request(request)

    return httpx.MockTransport(handler)


def seed_workflow() -> dict:
    return {
        "projects": [{"id": str(PROJECT_ID), "user_id": str(USER_ID)}],
        "papers": [
            {
                "id": str(PAPER_ID),
                "project_id": str(PROJECT_ID),
                "title": "LeNet-5",
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


def make_context(
    admin, results, *, budget_config=None, validate_ok=True, validate_issues=None
) -> WorkflowContext:
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    admin.storage.files["u/p.pdf"] = b"%PDF-1.4 fake"
    return WorkflowContext(
        admin=admin,
        agent=AgentClient(
            settings, transport=workflow_agent_transport(validate_ok, validate_issues)
        ),
        runner=RunnerClient(settings, transport=runner_transport(results)),
        settings=settings,
        budget_config=budget_config or BudgetConfig(),
    )


def _state(admin) -> dict:
    return initial_state(admin.tables["workflows"][0], BudgetConfig())


def _workflow(admin) -> dict:
    return admin.tables["workflows"][0]


async def _run_full_flow(ctx, admin, saver):
    state = _state(admin)
    await start_workflow(ctx, saver, state)
    assert _workflow(admin)["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value
    admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
    await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, state)
    assert _workflow(admin)["status"] == WorkflowStatus.AWAITING_PLAN_APPROVAL.value
    admin.tables["experiment_plans"][-1]["approved_at"] = "2026-01-01T00:00:00+00:00"
    await resume_workflow(ctx, saver, THREAD_ID, {"stage": "plan"}, state)


@pytest.mark.asyncio
async def test_happy_path_completes_with_traceable_report():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.98})])
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()

    assert _workflow(admin)["status"] == WorkflowStatus.COMPLETED.value
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert admin.tables["experiments"][0]["status"] == "succeeded"
    report = admin.tables["reproduction_reports"][0]["report_json"]
    assert report["references"]["run_id"] == run["id"]
    assert report["references"]["paper_id"] == str(PAPER_ID)
    assert len(report["references"]["generated_file_ids"]) == 5
    # every node on the success path traced once (classify/repair only on failure),
    # with no credentials or raw PDF in traces
    traced = {t["node_name"] for t in admin.tables["agent_traces"]}
    assert traced == ALL_NODES - {"classify_error", "repair_code"}
    for trace in admin.tables["agent_traces"]:
        blob = json.dumps(trace["input_json"]) + json.dumps(trace["output_json"])
        assert "pdf_b64" not in blob
        assert "service-test" not in blob and "anon-test" not in blob
        assert "sk-" not in blob
    # the extract node recorded its LLM usage on the trace and the budget
    extract_trace = next(
        t for t in admin.tables["agent_traces"] if t["node_name"] == "build_reproduction_spec"
    )
    assert extract_trace["model_name"] == "stub"
    assert extract_trace["token_usage"]["total_tokens"] == 150
    assert _workflow(admin)["budget_json"]["llm_calls"] == 1
    assert _workflow(admin)["budget_json"]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_human_review_interrupts_and_checkpoints():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [make_result(RunStatus.SUCCEEDED, 0)])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        assert _workflow(admin)["status"] == WorkflowStatus.AWAITING_SPEC_REVIEW.value
        assert _workflow(admin)["current_node"] == "human_review"
        # no experiment/run created yet: paused before planning
        assert admin.tables["experiments"] == []
        assert admin.tables["execution_runs"] == []
        from reproagent_api.workflow import build_workflow_graph

        graph = build_workflow_graph(ctx, saver)
        snapshot = graph.get_state({"configurable": {"thread_id": THREAD_ID}})
        assert snapshot.next == ("human_review",)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()


@pytest.mark.asyncio
async def test_import_error_auto_repairs_then_succeeds():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    failed = make_result(RunStatus.FAILED, 1, stderr="ModuleNotFoundError: No module named 'x'")
    success = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})
    ctx = make_context(admin, [failed, success])
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["error_category"] == "IMPORT_ERROR"
    assert {f["version"] for f in admin.tables["generated_files"]} == {1, 2}
    assert _workflow(admin)["status"] == WorkflowStatus.COMPLETED.value
    traced = {t["node_name"] for t in admin.tables["agent_traces"]}
    assert {"classify_error", "repair_code"} <= traced


@pytest.mark.asyncio
async def test_oom_triggers_new_plan_version():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    oom = make_result(RunStatus.FAILED, 137, stderr="RuntimeError: CUDA out of memory")
    success = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})
    ctx = make_context(admin, [oom, success])
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    plans = admin.tables["experiment_plans"]
    assert len(plans) == 2
    v2 = next(p for p in plans if p["version"] == 2)
    assert v2["origin"] == "oom_retry"
    assert v2["approved_at"]
    base_batch = plans[0]["config_json"]["training_config"]["batch_size"]["value"]
    assert v2["config_json"]["training_config"]["batch_size"]["value"] == max(1, base_batch // 2)
    assert _workflow(admin)["budget_json"]["replan_count"] == 1
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    # the run row must reference the plan it actually executed (the OOM replan),
    # not the original plan -- otherwise the report audit chain is inconsistent
    assert run["plan_id"] == v2["id"]
    assert run["plan_version"] == 2


@pytest.mark.asyncio
async def test_non_repairable_error_still_evaluates_and_reports():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    unknown = make_result(RunStatus.FAILED, 1, stderr="some totally novel failure xyz")
    ctx = make_context(admin, [unknown])
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "failed"
    assert run["error_category"] == "UNKNOWN_ERROR"
    assert len(admin.tables["generated_files"]) == 5  # no repair version
    report = admin.tables["reproduction_reports"][0]["report_json"]
    assert report["final_status"] == "EXECUTION_FAILED"
    # pipeline ran to completion (evaluated the failed run) -> workflow completed
    assert _workflow(admin)["status"] == WorkflowStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_static_validation_failure_repairs_until_budget_exhausted():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [make_result(RunStatus.SUCCEEDED, 0)], validate_ok=False)
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == WorkflowStatus.FAILED.value
    assert "static validation failed" in _workflow(admin)["error"]
    assert _workflow(admin)["budget_json"]["repair_count"] == 3
    assert admin.tables["execution_runs"][0]["status"] == "failed"
    assert admin.tables["execution_run_files"] == []  # never reached the runner
    assert admin.tables["reproduction_reports"] == []
    traced = {t["node_name"] for t in admin.tables["agent_traces"]}
    assert "repair_code" in traced and "classify_error" not in traced


@pytest.mark.asyncio
async def test_static_validation_failure_repairs_then_succeeds():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    admin.storage.files["u/p.pdf"] = b"%PDF-1.4 fake"
    base = workflow_agent_transport(validate_ok=False)
    calls = {"validate": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/codegen/validate":
            calls["validate"] += 1
            if calls["validate"] > 1:
                return httpx.Response(200, json={"ok": True, "issues": []})
        return base.handle_request(request)

    ctx = WorkflowContext(
        admin=admin,
        agent=AgentClient(settings, transport=httpx.MockTransport(handler)),
        runner=RunnerClient(
            settings,
            transport=runner_transport(
                [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})]
            ),
        ),
        settings=settings,
        budget_config=BudgetConfig(),
    )
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == WorkflowStatus.COMPLETED.value
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["repair_attempt"] == 1
    assert run["error_category"] == "SYNTAX_ERROR"
    # v1 never passed static validation, so only the repaired v2 is pinned
    assert {f["version"] for f in admin.tables["generated_files"]} == {2}
    repaired = [f for f in admin.tables["generated_files"] if f["version"] == 2]
    assert repaired[0]["change_reason"] == "auto-repair attempt 1"
    assert "static validation failed" in repaired[0]["related_error"]


@pytest.mark.asyncio
async def test_static_security_violation_is_not_auto_repaired():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(
        admin,
        [make_result(RunStatus.SUCCEEDED, 0)],
        validate_ok=False,
        validate_issues=[{"code": "banned_import", "message": "forbidden import: os"}],
    )
    saver = make_memory_checkpointer()
    try:
        await _run_full_flow(ctx, admin, saver)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == WorkflowStatus.FAILED.value
    assert _workflow(admin)["budget_json"]["repair_count"] == 0
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "failed"
    assert run["error_category"] == "UNKNOWN_ERROR"
    traced = {t["node_name"] for t in admin.tables["agent_traces"]}
    assert "repair_code" not in traced


@pytest.mark.asyncio
async def test_llm_call_budget_exhausted_fails():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [], budget_config=BudgetConfig(max_llm_calls=1))
    saver = make_memory_checkpointer()
    state = _state(admin)
    state["budget"] = BudgetState(llm_calls=1)  # already at the ceiling
    try:
        await start_workflow(ctx, saver, state)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == WorkflowStatus.FAILED.value
    assert "LLM call budget" in _workflow(admin)["error"]


@pytest.mark.asyncio
async def test_token_budget_exhausted_fails():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [], budget_config=BudgetConfig(max_total_tokens=10))
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == WorkflowStatus.FAILED.value
    assert "token budget" in _workflow(admin)["error"]


def test_redact_drops_credentials_and_keeps_usage():
    payload = {
        "api_key": "sk-supersecretkey1234567890",
        "pdf_b64": "AAAA",
        "supabase_service_role_key": "service-test",
        "nested": {"password": "hunter2", "authorization": "Bearer xyz"},
        "note": "key sk-abcdefghijklmnopqrstuvwx and sb_secret_abc123",
        "token_usage": {"total_tokens": 150, "prompt_tokens": 100},
        "model_name": "stub",
    }
    out = redact(payload)
    assert "api_key" not in out and "pdf_b64" not in out
    assert "supabase_service_role_key" not in out
    assert "password" not in out["nested"] and "authorization" not in out["nested"]
    assert "sk-" not in out["note"] and "sb_secret_" not in out["note"]
    assert "[REDACTED]" in out["note"]
    assert out["token_usage"]["total_tokens"] == 150  # usage is NOT redacted
    assert out["model_name"] == "stub"


def test_redact_truncates_long_strings():
    out = redact({"content": "x" * 5000})
    assert len(out["content"]) < 5000
    assert out["content"].endswith("…[truncated]")


def _cls(category, repairable=True):
    return ErrorClassification(category=category, reason="r", repairable=repairable)


def test_route_after_validate():
    config = BudgetConfig(max_repairs=3)
    assert route_after_validate({"validation_ok": True}) == "execute_code"
    assert route_after_validate({"budget_config": config}) == "persist_results"
    repairable = {
        "validation_ok": False,
        "budget_config": config,
        "repair_attempt": 0,
        "classification": _cls(ErrorCategory.SYNTAX_ERROR),
    }
    assert route_after_validate(repairable) == "repair_code"
    assert route_after_validate({**repairable, "repair_attempt": 3}) == "persist_results"
    assert (
        route_after_validate(
            {**repairable, "classification": _cls(ErrorCategory.UNKNOWN_ERROR, False)}
        )
        == "persist_results"
    )


def test_route_after_execute():
    assert route_after_execute({"budget_exhausted": True}) == "evaluate_result"
    assert route_after_execute({}) == "persist_results"
    ok = make_result(RunStatus.SUCCEEDED, 0)
    assert route_after_execute({"execution_result": ok}) == "evaluate_result"
    cancelled = make_result(RunStatus.CANCELLED, None)
    assert route_after_execute({"execution_result": cancelled}) == "persist_results"
    failed = make_result(RunStatus.FAILED, 1)
    assert route_after_execute({"execution_result": failed}) == "classify_error"


def test_route_after_classify_bounds_repairs_and_replans():
    config = BudgetConfig(max_repairs=3, max_replans=2)
    base = {"budget_config": config, "repair_attempt": 0, "replan_count": 0}
    assert (
        route_after_classify({**base, "classification": _cls(ErrorCategory.IMPORT_ERROR)})
        == "repair_code"
    )
    assert (
        route_after_classify({**base, "classification": _cls(ErrorCategory.UNKNOWN_ERROR, False)})
        == "evaluate_result"
    )
    assert (
        route_after_classify(
            {**base, "repair_attempt": 3, "classification": _cls(ErrorCategory.IMPORT_ERROR)}
        )
        == "evaluate_result"
    )
    assert (
        route_after_classify(
            {**base, "replan_count": 2, "classification": _cls(ErrorCategory.OUT_OF_MEMORY)}
        )
        == "evaluate_result"
    )
    assert (
        route_after_classify({**base, "classification": _cls(ErrorCategory.OUT_OF_MEMORY)})
        == "repair_code"
    )


def test_budget_exceeded_is_exception():
    assert issubclass(BudgetExceeded, Exception)
    state = {"budget": BudgetState(experiment_seconds=10)}
    from reproagent_api.workflow.harness import time_budget_exhausted

    assert time_budget_exhausted(state, BudgetConfig(max_experiment_seconds=5))
    assert not time_budget_exhausted(state, BudgetConfig(max_experiment_seconds=60))


@pytest.mark.asyncio
async def test_execute_code_skips_run_cancelled_while_queued():
    from reproagent_api.workflow.nodes import execute_code

    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    run_id = str(uuid4())
    experiment_id = str(uuid4())
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": experiment_id,
            "status": "cancelled",
            "plan_id": str(uuid4()),
            "plan_version": 1,
            "code_version": 0,
            "repair_attempt": 0,
        }
    )
    # Empty runner queue: start_run would raise if the node tried to execute.
    ctx = make_context(admin, [])
    state = _state(admin)
    state["run_id"] = run_id
    state["experiment_id"] = experiment_id
    try:
        result = await execute_code(state, ctx)
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert result["execution_result"].status is RunStatus.CANCELLED
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    assert admin.tables["generated_files"] == []


def _confirm_delegated_card(admin, *, actor=USER_ID):
    from reproagent_api.services.card_edits import delegate_card

    base = admin.tables["reproduction_specs"][-1]
    card = ReproductionCard.model_validate(base["spec_json"])
    # Missing learning rate remains missing in the authorized Card; only the
    # later validated Plan recommendation may propose an executable value.
    card.hyperparameters.pop("learning_rate", None)
    card.optimizer = card.optimizer.model_validate({"status": "missing", "value": None})
    card = delegate_card(card, actor=actor)
    row = {**base, "id": str(uuid4()), "version": base["version"] + 1,
           "status": "confirmed", "spec_json": card.model_dump(mode="json")}
    admin.tables["reproduction_specs"].append(row)
    return row


def _recommend_transport(seen, failure=None):
    base = workflow_agent_transport()

    def handler(request):
        if request.url.path != "/v1/recommend":
            return base.handle_request(request)
        context = ReviewRequest.model_validate(json.loads(request.content))
        seen.append(context)
        if failure == "provider":
            raise RuntimeError("provider leaked sk-abcdefghijklmnopqrstuvwx")
        fields = context.review_fields()
        recommendations = [FieldRecommendation(
            id=str(uuid4()), field_path=f.field_path, action="accept", suggested_value=f.value,
            confidence=0.95, reasoning="Validated bounded proposal", risk="Check convergence",
        ) for f in fields]
        response = RecommendResponse(
            context_hash=context.context_hash(), recommendations=recommendations,
            usages=[LLMUsage(model_name="review-stub", prompt_tokens=20,
                             completion_tokens=10, total_tokens=30)],
        )
        if failure == "context":
            response.context_hash = "0" * 64
        elif failure == "missing":
            response.recommendations = []
        elif failure == "accept_value":
            response.recommendations[0].suggested_value = 999
        elif failure == "duplicate_id":
            for recommendation in response.recommendations:
                recommendation.id = "duplicate"
        elif failure == "preselection":
            response.preselected_paths = ["training_config.learning_rate"]
        return httpx.Response(200, json=response.model_dump(mode="json"))

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_delegated_card_recommends_approves_skips_plan_gate_and_executes():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})])
    saver = make_memory_checkpointer()
    seen = []
    try:
        await start_workflow(ctx, saver, _state(admin))
        card_row = _confirm_delegated_card(admin)
        await ctx.agent.aclose()
        ctx.agent = AgentClient(ctx.settings, transport=_recommend_transport(seen))
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "completed"
    assert len(seen) == 1 and seen[0].scope == str(USER_ID)
    assert seen[0].card.model_dump(mode="json") == card_row["spec_json"]
    row = admin.tables["experiment_plans"][0]
    assert row["version"] == 1 and row["approved_at"]
    plan = ExperimentPlan.model_validate(row["config_json"])
    assert plan.delegated_by == USER_ID
    assert plan.delegated_at == seen[0].card.delegated_at
    assert plan.approval_ready(USER_ID) and plan.unconfirmed_critical()
    param = plan.training_config.learning_rate
    assert param.confirmed_by.value == "auto" and not param.human_confirmed
    assert param.source.value == "inferred" and param.requires_confirmation
    assert {r["field_path"] for r in row["changes_json"]} >= {
        "review_mode", "training_config.learning_rate",
    }
    assert len(admin.tables["execution_runs"]) == 1
    assert _workflow(admin)["budget_json"]["llm_calls"] == 2
    assert _workflow(admin)["budget_json"]["total_tokens"] == 180
    review_trace = next(t for t in admin.tables["agent_traces"]
                        if t["node_name"] == "plan_experiment")
    assert review_trace["token_usage"]["total_tokens"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "provider", "context", "missing", "accept_value", "duplicate_id", "preselection",
])
async def test_delegated_recommendation_failure_never_approves_or_executes(failure):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        _confirm_delegated_card(admin)
        await ctx.agent.aclose()
        ctx.agent = AgentClient(ctx.settings, transport=_recommend_transport([], failure))
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "failed"
    assert admin.tables["experiment_plans"] == []
    assert admin.tables["experiments"] == []
    assert admin.tables["execution_runs"] == []
    assert "sk-" not in json.dumps(admin.tables)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["draft", "wrong_owner", "wrong_project"])
async def test_spec_resume_rechecks_database_authority(problem):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        if problem == "wrong_owner":
            _confirm_delegated_card(admin, actor=uuid4())
        elif problem == "wrong_project":
            admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
            admin.tables["papers"][0]["project_id"] = str(uuid4())
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "failed"
    assert admin.tables["experiment_plans"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ReviewMode.EXPERT, ReviewMode.ASSISTED])
async def test_ordinary_modes_interrupt_and_recheck_plan_approval(mode):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [])
    saver = make_memory_checkpointer()
    try:
        await start_workflow(ctx, saver, _state(admin))
        spec = admin.tables["reproduction_specs"][-1]
        spec["status"] = "confirmed"
        spec["spec_json"]["review_mode"] = mode.value
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
        assert _workflow(admin)["status"] == "awaiting_plan_approval"
        assert admin.tables["experiment_plans"][0]["config_json"]["review_mode"] == mode.value
        # An untrusted resume does not substitute for approval persisted by API.
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "plan"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "failed"
    assert admin.tables["execution_runs"] == []


@pytest.mark.asyncio
async def test_agent_cannot_delegate_an_ordinary_card():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [])
    saver = make_memory_checkpointer()
    base = workflow_agent_transport()

    def forged(request):
        response = base.handle_request(request)
        if request.url.path == "/v1/plan":
            body = response.json()
            body["plan"].update(review_mode="delegated", delegated_by=str(USER_ID),
                                delegated_at=datetime.now(UTC).isoformat())
            return httpx.Response(200, json=body)
        return response

    try:
        await start_workflow(ctx, saver, _state(admin))
        admin.tables["reproduction_specs"][-1]["status"] = "confirmed"
        await ctx.agent.aclose()
        ctx.agent = AgentClient(ctx.settings, transport=httpx.MockTransport(forged))
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "awaiting_plan_approval"
    row = admin.tables["experiment_plans"][0]
    assert row["approved_at"] is None
    assert row["config_json"]["review_mode"] == "assisted"
    assert row["config_json"]["delegated_by"] is None
    assert admin.tables["execution_runs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("config,expected_calls", [
    (BudgetConfig(max_llm_calls=1), 0),
    (BudgetConfig(max_total_tokens=160), 1),
])
async def test_delegated_review_respects_call_and_token_budgets(config, expected_calls):
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = make_context(admin, [], budget_config=config)
    saver = make_memory_checkpointer()
    seen = []
    try:
        await start_workflow(ctx, saver, _state(admin))
        _confirm_delegated_card(admin)
        await ctx.agent.aclose()
        ctx.agent = AgentClient(ctx.settings, transport=_recommend_transport(seen))
        await resume_workflow(ctx, saver, THREAD_ID, {"stage": "spec"}, _state(admin))
    finally:
        await ctx.agent.aclose()
        await ctx.runner.aclose()
    assert _workflow(admin)["status"] == "failed"
    assert "budget exhausted" in _workflow(admin)["error"]
    assert len(seen) == expected_calls
    assert admin.tables["experiments"] == []
    assert admin.tables["experiment_plans"] == []
    assert admin.tables["execution_runs"] == []


def test_trace_and_workflow_errors_are_redacted():
    admin = FakeAdmin(seed_workflow(), {str(PROJECT_ID): str(USER_ID)})
    ctx = WorkflowContext(admin, None, None, None)
    secret = "provider sk-abcdefghijklmnopqrstuvwx sb_secret_abcdef"
    ctx.write_trace(_state(admin), "plan_experiment", {"error": secret}, {"error": secret},
                    1, None, None, secret, 0)
    ctx.persist_workflow(_state(admin), {"error": secret}, "plan_experiment")
    ctx.finalize_workflow(_state(admin), WorkflowStatus.FAILED, secret)
    assert "sk-" not in json.dumps(admin.tables)
    assert "sb_secret_" not in json.dumps(admin.tables)
