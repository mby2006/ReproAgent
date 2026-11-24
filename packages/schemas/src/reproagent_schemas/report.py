"""Traceable evaluation contracts: paper claims vs pinned code vs real execution.

Every check carries evidence for BOTH sides (paper quote/page, code file/field,
plan param, run metrics/audit). Experiment values may only come from a real
execution run; paper values only from grounded card evidence. The final status
enum deliberately has no "full reproduction" verdict: a scaled MVP run can at
best be METHOD_VALIDATED, never a claim of full paper equivalence.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from .card import ReproductionCard
from .common import ReviewDecision, ReviewMode, SchemaModel
from .execution import CodeFile, ErrorCategory, RunAudit, RunStatus
from .plan import Assumption, Deviation, ExperimentPlan


class CheckStatus(StrEnum):
    MATCHED = "matched"
    DEVIATED = "deviated"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class FinalStatus(StrEnum):
    METHOD_VALIDATED = "METHOD_VALIDATED"
    SCALED_RUN_COMPLETED = "SCALED_RUN_COMPLETED"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    DEVIATION_FOUND = "DEVIATION_FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class MethodItem(StrEnum):
    ARCHITECTURE = "architecture"
    LAYER_CONFIGURATION = "layer_configuration"
    ACTIVATION = "activation"
    SUBSAMPLING = "subsampling"
    OUTPUT_LAYER = "output_layer"
    LOSS_FUNCTION = "loss_function"
    PREPROCESSING = "preprocessing"
    DATASET = "dataset"


class ExperimentItem(StrEnum):
    TRAINING_SAMPLES = "training_samples"
    TEST_SAMPLES = "test_samples"
    EPOCHS = "epochs"
    BATCH_SIZE = "batch_size"
    OPTIMIZER = "optimizer"
    LEARNING_RATE = "learning_rate"
    RANDOM_SEED = "random_seed"
    DATASET_FINGERPRINT = "dataset_fingerprint"
    RUNTIME_ENVIRONMENT = "runtime_environment"


class EvidenceRef(SchemaModel):
    """Pointer to the source backing one side of a check/comparison."""

    kind: str = Field(pattern=r"^(paper|code|plan|metrics|audit|config)$")
    quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    file_path: str | None = None
    field_path: str | None = None
    detail: str | None = None


class MethodCheck(SchemaModel):
    item: MethodItem
    status: CheckStatus
    paper_value: str | None = None
    implementation_value: str | None = None
    paper_evidence: EvidenceRef | None = None
    implementation_evidence: EvidenceRef | None = None
    note: str = Field(min_length=1)


class ExperimentCheck(SchemaModel):
    item: ExperimentItem
    status: CheckStatus
    plan_value: str | int | float | None = None
    actual_value: str | int | float | None = None
    plan_evidence: EvidenceRef | None = None
    actual_evidence: EvidenceRef | None = None
    note: str = Field(min_length=1)


class MetricComparison(SchemaModel):
    metric: str = Field(min_length=1)
    paper_value: float | None = None
    experiment_value: float | None = None
    absolute_difference: float | None = None
    relative_difference: float | None = None
    percentage_point_difference: float | None = None
    unit: str = Field(min_length=1)
    comparison_status: CheckStatus
    tolerance: float | None = Field(default=None, ge=0)
    paper_evidence: EvidenceRef | None = None
    experiment_evidence: EvidenceRef | None = None
    note: str | None = None

    @model_validator(mode="after")
    def check_difference_consistency(self) -> "MetricComparison":
        if self.paper_value is None or self.experiment_value is None:
            if (
                self.absolute_difference is not None
                or self.relative_difference is not None
                or self.percentage_point_difference is not None
            ):
                raise ValueError("differences require both paper and experiment values")
            if self.comparison_status in (CheckStatus.MATCHED, CheckStatus.DEVIATED):
                raise ValueError("matched/deviated comparison requires both values")
        return self


class ReportReferences(SchemaModel):
    """Every report pins the exact paper/spec/plan/code/run it evaluated."""

    paper_id: UUID
    spec_version_id: UUID
    spec_version: int = Field(ge=1)
    experiment_id: UUID
    plan_id: UUID
    plan_version: int = Field(ge=1)
    code_version: int = Field(ge=1)
    code_hash: str | None = None
    run_id: UUID
    generated_file_ids: list[UUID] = Field(default_factory=list)
    review_trace_ids: list[UUID] = Field(default_factory=list)


class ParameterComparison(SchemaModel):
    key: str
    label: str
    paper: str | None = None
    plan: str | None = None
    actual: str | None = None
    paper_evidence: EvidenceRef | None = None
    actual_evidence: EvidenceRef | None = None
    decision: str | None = None
    note: str


class EvaluationReport(SchemaModel):
    references: ReportReferences
    target_metric: str | None = None
    parameter_comparisons: list[ParameterComparison] = Field(default_factory=list)
    final_status: FinalStatus
    summary: str = Field(min_length=1)
    reader_summary: str | None = None
    method_checks: list[MethodCheck] = Field(default_factory=list)
    experiment_checks: list[ExperimentCheck] = Field(default_factory=list)
    metric_comparisons: list[MetricComparison] = Field(default_factory=list)
    scaling_deviations: list[Deviation] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    review_mode: ReviewMode | None = None
    delegated_at: datetime | None = None
    delegated_by: UUID | None = None
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    review_disclaimer: str | None = None


class EvaluateRequest(SchemaModel):
    """Worker -> Agent contract. All execution data comes from a real run."""

    references: ReportReferences
    card: ReproductionCard
    plan: ExperimentPlan
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    paper_title: str | None = None
    code_files: list[CodeFile] = Field(default_factory=list)
    run_status: RunStatus
    metrics: dict[str, Any] = Field(default_factory=dict)
    audit: RunAudit | None = None
    execution_time: float | None = Field(default=None, ge=0)
    exit_code: int | None = None
    error: str | None = None
    error_category: ErrorCategory | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    artifacts: list[str] = Field(default_factory=list)


class EvaluateResponse(SchemaModel):
    report: EvaluationReport
    markdown: str = Field(min_length=1)
