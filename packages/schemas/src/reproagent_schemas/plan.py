"""Scaled-down experiment plan derived from a Reproduction Card.

Every key parameter carries provenance (paper_explicit / inferred /
human_confirmed / mvp_scaling). MVP scaling defaults are explicitly tagged so
they can never be mistaken for the paper's own settings. Parameters the paper
does not state are never silently defaulted: they become flagged proposals that
require human confirmation before the plan can be approved.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator

from .card import ReproductionCard
from .common import (
    ConfirmationSource,
    ManualReviewMode,
    ReviewDecision,
    ReviewMode,
    SchemaModel,
    check_confirmation,
    check_delegation,
    utcnow,
)
from .evidence import EvidenceStatus


class _PlanModel(SchemaModel):
    """Plan models serialize by alias so the wire key `model_config` survives
    Pydantic's reserved `model_config` attribute (the field is `model_cfg`)."""

    model_config = ConfigDict(populate_by_name=True)

    def model_dump(self, **kwargs):
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs):
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)


class ParamSource(StrEnum):
    PAPER_EXPLICIT = "paper_explicit"
    INFERRED = "inferred"
    HUMAN_CONFIRMED = "human_confirmed"
    MVP_SCALING = "mvp_scaling"


class PlanOrigin(StrEnum):
    GENERATED = "generated"
    USER_EDIT = "user_edit"
    OOM_RETRY = "oom_retry"


class AssumptionKind(StrEnum):
    INFERRED = "inferred"
    MISSING_PROPOSAL = "missing_proposal"
    MVP_SCALING = "mvp_scaling"
    NONSTANDARD_CHOICE = "nonstandard_choice"


class RiskTier(StrEnum):
    """How much a wrong assumption hurts the reproduction.

    critical: changes the experiment's essence (optimizer/lr/loss/architecture/
    target metric/dataset identity) -- always needs an explicit decision.
    routine: small effect with a safe default -- batch-accept / auto-accept allowed.
    info: informational only (MVP scaling notes) -- never blocks approval.
    """

    CRITICAL = "critical"
    ROUTINE = "routine"
    INFO = "info"


class PlanParam[T](SchemaModel):
    """A concrete, executable parameter value with auditable provenance."""

    value: T
    source: ParamSource
    paper_value: T | None = None
    evidence_quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    rationale: str | None = None
    requires_confirmation: bool = False
    human_confirmed: bool = False
    confirmed_by: ConfirmationSource | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def check_provenance(self) -> "PlanParam[T]":
        check_confirmation(self.confirmed_by, self.human_confirmed)
        if (
            self.confirmed_by in (ConfirmationSource.AUTO, ConfirmationSource.AI_ASSISTED)
            and self.source is not ParamSource.INFERRED
        ):
            raise ValueError("AI acceptance must remain inferred")
        if self.source is ParamSource.PAPER_EXPLICIT:
            if not self.evidence_quote or self.source_page is None:
                raise ValueError("paper_explicit param requires evidence_quote and source_page")
        if self.source is ParamSource.INFERRED and not self.rationale:
            raise ValueError("inferred param requires a rationale")
        if self.evidence_quote is not None and not self.evidence_quote.strip():
            raise ValueError("evidence_quote must not be blank")
        return self


class Assumption(SchemaModel):
    """A non-paper-explicit decision surfaced for human review."""

    field_path: str = Field(min_length=1)
    kind: AssumptionKind
    value: Any
    rationale: str = Field(min_length=1)
    requires_confirmation: bool = True
    human_confirmed: bool = False
    confirmed_by: ConfirmationSource | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    risk_tier: RiskTier = RiskTier.CRITICAL
    auto_acceptable: bool = False
    card_status: EvidenceStatus | None = None
    evidence_quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_provenance(self) -> "Assumption":
        check_confirmation(self.confirmed_by, self.human_confirmed)
        return self


class Deviation(SchemaModel):
    """Where the MVP plan intentionally differs from the paper's setting."""

    field_path: str = Field(min_length=1)
    paper_value: str | None = None
    plan_value: str
    reason: str = Field(min_length=1)


class DatasetConfig(SchemaModel):
    name: str = "mnist"
    num_classes: int = Field(default=10, gt=0)
    input_shape: list[int] = Field(default_factory=lambda: [1, 28, 28])
    train_subset_size: int = Field(gt=0)
    test_subset_size: int = Field(gt=0)
    full_train_size: int | None = Field(default=None, gt=0)
    full_test_size: int | None = Field(default=None, gt=0)
    source: ParamSource = ParamSource.PAPER_EXPLICIT
    evidence_quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)


class ModelConfig(SchemaModel):
    architecture: str = "lenet-5"
    input_shape: list[int] = Field(default_factory=lambda: [1, 28, 28])
    num_classes: int = Field(default=10, gt=0)
    description: str | None = None
    source: ParamSource = ParamSource.PAPER_EXPLICIT
    evidence_quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)


class PreprocessingStep(SchemaModel):
    name: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    source: ParamSource
    rationale: str | None = None


class PreprocessingConfig(SchemaModel):
    steps: list[PreprocessingStep] = Field(default_factory=list)


class TrainingConfig(SchemaModel):
    optimizer: PlanParam[str]
    learning_rate: PlanParam[float]
    loss_function: PlanParam[str]
    batch_size: PlanParam[int]
    epochs: PlanParam[int]
    weight_decay: PlanParam[float] | None = None
    seed: int = 42


class EvaluationConfig(SchemaModel):
    metrics: list[str] = Field(min_length=1)
    target_metric: str = Field(min_length=1)
    reported_value: float | None = None
    reported_condition: str | None = None
    reported_source_page: int | None = Field(default=None, ge=1)
    tolerance: float = Field(ge=0)
    comparison_note: str = Field(min_length=1)


class RuntimeBudget(SchemaModel):
    max_seconds: int = Field(default=600, gt=0)
    device: str = "cpu"
    seed: int = 42


class ExpectedRuntime(SchemaModel):
    estimated_seconds: int = Field(ge=0)
    basis: str = Field(min_length=1)


class ResourceEstimates(SchemaModel):
    device: str = "cpu"
    estimated_memory_mb: int | None = Field(default=None, ge=0)
    estimated_disk_mb: int | None = Field(default=None, ge=0)
    note: str | None = None


class ExperimentPlan(_PlanModel):
    review_mode: ReviewMode = ReviewMode.ASSISTED
    delegated_at: datetime | None = None
    delegated_by: UUID | None = None
    scaling_profile: str = "mvp_lenet5_mnist_v1"
    rationale: str = Field(min_length=1)
    dataset_config: DatasetConfig
    model_cfg: ModelConfig = Field(alias="model_config")
    preprocessing_config: PreprocessingConfig
    training_config: TrainingConfig
    evaluation_config: EvaluationConfig
    runtime_budget: RuntimeBudget
    expected_runtime: ExpectedRuntime
    resource_estimates: ResourceEstimates
    assumptions: list[Assumption] = Field(default_factory=list)
    deviations_from_paper: list[Deviation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_authorization(self) -> "ExperimentPlan":
        check_delegation(self.review_mode, self.delegated_at, self.delegated_by)
        return self

    def approval_ready(self, actor: UUID | None = None) -> bool:
        if self.review_mode is not ReviewMode.DELEGATED:
            return not self.unconfirmed_critical()
        if self.delegated_at is None or self.delegated_by is None:
            return False
        if self.delegated_at.tzinfo is None or self.delegated_at.utcoffset() is None:
            return False
        if actor is not None and actor != self.delegated_by:
            return False
        return all(
            a.human_confirmed or a.confirmed_by is ConfirmationSource.AUTO
            for a in self.assumptions
            if a.requires_confirmation and a.risk_tier is not RiskTier.INFO
        )

    def unconfirmed_required(self) -> list[Assumption]:
        return [a for a in self.assumptions if a.requires_confirmation and not a.human_confirmed]

    def unconfirmed_critical(self) -> list[Assumption]:
        """Only critical-tier assumptions block approval; routine items may be
        batch-accepted or left to safe defaults without gating the run."""
        return [
            a
            for a in self.assumptions
            if a.requires_confirmation
            and not a.human_confirmed
            and a.risk_tier is RiskTier.CRITICAL
        ]


class PlanRequest(SchemaModel):
    """API -> Agent contract. Optional overrides tune the MVP scaling profile."""

    card: ReproductionCard
    train_subset_size: int | None = Field(default=None, gt=0)
    test_subset_size: int | None = Field(default=None, gt=0)
    epochs: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    max_seconds: int | None = Field(default=None, gt=0)


class PlanResponse(_PlanModel):
    plan: ExperimentPlan


class PlanChange(SchemaModel):
    field_path: str = Field(pattern=r"^[a-z_]+(\.[A-Za-z0-9_\-]+)*$")
    value: Any
    human_confirmed: bool = True


class PlanChangeRecord(ReviewDecision):
    """Backward-compatible plan-specific name for an auditable review decision."""


class PlanEditRequest(SchemaModel):
    base_version: int = Field(ge=1)
    changes: list[PlanChange] = Field(default_factory=list)
    review_mode: ManualReviewMode | None = None
    change_reason: str = Field(min_length=1)
    origin: PlanOrigin = PlanOrigin.USER_EDIT
    related_error: str | None = None
    related_run_id: UUID | None = None

    @model_validator(mode="after")
    def require_change(self) -> "PlanEditRequest":
        if not self.changes and self.review_mode is None:
            raise ValueError("changes or review_mode is required")
        return self


class PlanVersion(_PlanModel):
    id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID
    version: int = Field(ge=1)
    origin: PlanOrigin
    parent_version_id: UUID | None = None
    plan: ExperimentPlan
    change_reason: str | None = None
    changes: list[PlanChangeRecord] = Field(default_factory=list)
    related_error: str | None = None
    related_run_id: UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
