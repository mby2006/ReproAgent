from uuid import uuid4

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    AWAITING_WORKFLOW_STATUSES,
    TERMINAL_WORKFLOW_STATUSES,
    BudgetConfig,
    BudgetState,
    LLMUsage,
    NodeTrace,
    ResourceBudget,
    ResumeStage,
    Workflow,
    WorkflowResumeRequest,
    WorkflowStatus,
)


def test_workflow_status_has_exactly_thirteen_states():
    assert len(WorkflowStatus) == 13
    assert {s.value for s in WorkflowStatus} == {
        "uploaded",
        "parsing",
        "awaiting_spec_review",
        "planning",
        "awaiting_plan_approval",
        "generating_code",
        "validating_code",
        "executing",
        "repairing",
        "evaluating",
        "completed",
        "failed",
        "cancelled",
    }


def test_terminal_and_awaiting_sets():
    assert TERMINAL_WORKFLOW_STATUSES == {
        WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED,
    }
    assert AWAITING_WORKFLOW_STATUSES == {
        WorkflowStatus.AWAITING_SPEC_REVIEW,
        WorkflowStatus.AWAITING_PLAN_APPROVAL,
    }
    assert not (TERMINAL_WORKFLOW_STATUSES & AWAITING_WORKFLOW_STATUSES)


def test_llm_usage_defaults_and_nonnegative():
    usage = LLMUsage()
    assert usage.total_tokens == 0
    with pytest.raises(ValidationError):
        LLMUsage(prompt_tokens=-1)


def test_budget_state_accumulates_llm_and_time():
    state = BudgetState()
    state.record_llm(LLMUsage(model_name="qwen", prompt_tokens=100, completion_tokens=50))
    state.record_llm(LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    assert state.llm_calls == 2
    assert state.prompt_tokens == 110
    assert state.completion_tokens == 55
    assert state.total_tokens == 165
    state.record_experiment_time(12.5)
    state.record_experiment_time(None)
    state.record_experiment_time(-3)
    assert state.experiment_seconds == 12.5


def test_budget_state_total_tokens_fallback():
    state = BudgetState()
    state.record_llm(LLMUsage(prompt_tokens=7, completion_tokens=3, total_tokens=0))
    assert state.total_tokens == 10


def test_budget_config_defaults_and_bounds():
    cfg = BudgetConfig()
    assert cfg.max_repairs == 3
    assert cfg.max_llm_calls == 8
    assert isinstance(cfg.resource_budget, ResourceBudget)
    with pytest.raises(ValidationError):
        BudgetConfig(max_llm_calls=0)
    with pytest.raises(ValidationError):
        BudgetConfig(max_repairs=11)


def test_node_trace_structure():
    trace = NodeTrace(
        node_name="extract_paper",
        workflow_id=uuid4(),
        duration_ms=120,
        model_name="qwen",
        token_usage=LLMUsage(total_tokens=42),
        retry_count=1,
    )
    assert trace.input_json == {}
    assert trace.error is None
    assert trace.token_usage.total_tokens == 42
    with pytest.raises(ValidationError):
        NodeTrace(node_name="")


def test_workflow_defaults():
    wf = Workflow(id=uuid4(), project_id=uuid4(), paper_id=uuid4(), thread_id="t-1")
    assert wf.status is WorkflowStatus.UPLOADED
    assert wf.current_node is None
    assert wf.experiment_id is None
    assert isinstance(wf.budget, BudgetState)
    assert wf.budget.llm_calls == 0


def test_workflow_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Workflow(
            id=uuid4(),
            project_id=uuid4(),
            paper_id=uuid4(),
            thread_id="t",
            secret_key="sk-should-not-exist",
        )


def test_resume_request_stage_enum():
    assert WorkflowResumeRequest(stage="spec").stage is ResumeStage.SPEC
    assert WorkflowResumeRequest(stage="plan").stage is ResumeStage.PLAN
    with pytest.raises(ValidationError):
        WorkflowResumeRequest(stage="execute")