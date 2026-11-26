"""Shared state for the 15-node reproduction workflow graph.

The graph lives in the API/worker trust domain: it owns every database write and
drives the Runner. Compute is delegated to the Agent over HTTP; code execution
happens only inside the Runner. State is checkpointed after each node so an
interrupted or crashed workflow resumes from the last completed node.
"""

from typing import TypedDict

from reproagent_schemas import (
    BudgetConfig,
    BudgetState,
    CodeBundle,
    ErrorClassification,
    EvaluationReport,
    ExperimentPlan,
    LLMUsage,
    ParsedDocument,
    ReproductionCard,
    RunResult,
)


class WorkflowState(TypedDict, total=False):
    workflow_id: str
    thread_id: str
    project_id: str
    paper_id: str
    paper_title: str

    pdf_b64: str
    document: ParsedDocument
    card_raw: dict
    card: ReproductionCard
    usages: list[LLMUsage]

    spec_version_id: str
    spec_version: int
    spec_confirmed: bool

    experiment_id: str
    plan: ExperimentPlan
    plan_id: str
    plan_version: int
    plan_approved: bool

    code_bundle: CodeBundle
    code_version: int
    validation_ok: bool

    run_id: str
    runner_run_id: str
    execution_result: RunResult
    classification: ErrorClassification
    repair_attempt: int
    replan_count: int
    budget_exhausted: bool

    report: EvaluationReport
    markdown: str

    budget_config: BudgetConfig
    budget: BudgetState

    status: str
    current_node: str
    error: str
    support_code: str


def initial_state(workflow: dict, budget_config: BudgetConfig) -> WorkflowState:
    return WorkflowState(
        workflow_id=str(workflow["id"]),
        thread_id=workflow["thread_id"],
        project_id=str(workflow["project_id"]),
        paper_id=str(workflow["paper_id"]),
        budget_config=budget_config,
        budget=BudgetState(),
        status="uploaded",
        repair_attempt=0,
        replan_count=0,
        code_version=0,
        plan_version=0,
        spec_version=0,
    )
