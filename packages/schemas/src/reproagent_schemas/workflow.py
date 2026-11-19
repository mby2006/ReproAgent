"""Contracts for the unified LangGraph reproduction workflow.

A workflow ties the whole chain (paper -> spec -> plan -> code -> run -> report)
to a single checkpointed thread and a 12-state status machine. Budget and trace
models make LLM/token/time/resource ceilings and per-node observability explicit.
No credential material may ever appear in a NodeTrace.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .common import SchemaModel, utcnow
from .execution import ResourceBudget


class WorkflowStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    AWAITING_SPEC_REVIEW = "awaiting_spec_review"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    GENERATING_CODE = "generating_code"
    VALIDATING_CODE = "validating_code"
    EXECUTING = "executing"
    REPAIRING = "repairing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_WORKFLOW_STATUSES = frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.FAILED})
AWAITING_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.AWAITING_SPEC_REVIEW, WorkflowStatus.AWAITING_PLAN_APPROVAL}
)


class ResumeStage(StrEnum):
    SPEC = "spec"
    PLAN = "plan"


class LLMUsage(SchemaModel):
    """Token accounting for a single LLM call (provider-reported, never guessed)."""

    model_name: str | None = None
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class BudgetConfig(SchemaModel):
    """Hard ceilings for a workflow. Container limits mirror the sandbox budget."""

    max_llm_calls: int = Field(default=8, gt=0, le=100)
    max_total_tokens: int = Field(default=200_000, gt=0)
    max_experiment_seconds: int = Field(default=600, gt=0, le=3600)
    # SQL enforces execution_runs.repair_attempt between 0 and 3; keep the
    # ceiling aligned so a configured budget can never drive a check violation.
    max_repairs: int = Field(default=3, ge=0, le=3)
    max_replans: int = Field(default=2, ge=0, le=10)
    resource_budget: ResourceBudget = Field(default_factory=ResourceBudget)


class BudgetState(SchemaModel):
    """Accumulated consumption across the workflow lifetime."""

    llm_calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    experiment_seconds: float = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)

    def record_llm(self, usage: LLMUsage) -> None:
        self.llm_calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens or (
            usage.prompt_tokens + usage.completion_tokens
        )

    def record_experiment_time(self, seconds: float | None) -> None:
        if seconds and seconds > 0:
            self.experiment_seconds += seconds


class NodeTrace(SchemaModel):
    """Append-only per-node observability record. Credentials must be redacted."""

    node_name: str = Field(min_length=1)
    workflow_id: UUID | None = None
    experiment_id: UUID | None = None
    run_id: UUID | None = None
    input_json: dict = Field(default_factory=dict)
    output_json: dict = Field(default_factory=dict)
    duration_ms: int | None = Field(default=None, ge=0)
    model_name: str | None = None
    token_usage: LLMUsage | None = None
    error: str | None = None
    retry_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)


class Workflow(SchemaModel):
    """Full workflow detail returned by the API (status machine + budget + refs)."""

    id: UUID
    project_id: UUID
    paper_id: UUID
    thread_id: str
    status: WorkflowStatus = WorkflowStatus.UPLOADED
    current_node: str | None = None
    experiment_id: UUID | None = None
    run_id: UUID | None = None
    spec_version_id: UUID | None = None
    plan_id: UUID | None = None
    budget: BudgetState = Field(default_factory=BudgetState)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class WorkflowSummary(SchemaModel):
    id: UUID
    paper_id: UUID
    status: WorkflowStatus
    current_node: str | None = None
    created_at: datetime


class WorkflowAccepted(SchemaModel):
    workflow_id: UUID
    thread_id: str
    status: WorkflowStatus = WorkflowStatus.UPLOADED


class WorkflowResumeRequest(SchemaModel):
    stage: ResumeStage