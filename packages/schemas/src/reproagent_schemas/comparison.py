"""Paired optimizer comparison: two study groups, one controlled experiment.

A comparison is NOT a reproduction: it answers one question the paper poses by
running two configurations that differ only in the factor under test. The
contract encodes the hard requirements from the product plan:

* both groups bind to a paper-owned reference (page/table), never invented;
* every shared condition is explicit and verified after execution, not assumed;
* a difference is only computed when both runs succeeded AND the pair
  verification passed (same split, initial weights, sample order, budget);
* accuracy is recomputed from raw correct/evaluated counts, never copied from
  the training script's own claim;
* a scaled-down comparison can never be reported as a full reproduction.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .card import ReproductionCard
from .common import SchemaModel, utcnow
from .execution import RunAudit, RunStatus
from .parsed import ParsedDocument
from .plan import ExperimentPlan, PlanParam, RiskTier
from .report import CheckStatus, EvidenceRef


class ComparisonStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ComparisonOrigin(StrEnum):
    NEW = "new"
    RERUN = "rerun"
    RERUN_CURRENT_ENV = "rerun_current_env"


class PlanVersionOrigin(StrEnum):
    GENERATED = "generated"
    USER_EDIT = "user_edit"
    SCALING_CHANGE = "scaling_change"
    RERUN = "rerun"


class GroupKey(StrEnum):
    A = "a"
    B = "b"


class PendingStatus(StrEnum):
    PENDING = "pending"
    ADOPTED = "adopted"
    PROVIDED = "provided"


class PairItem(StrEnum):
    DATASET_SPLIT = "dataset_split"
    INITIAL_WEIGHTS = "initial_weights"
    SAMPLE_ORDER = "sample_order"
    TRAINING_BUDGET = "training_budget"
    EVALUATION = "evaluation"
    CODE = "code"


class PaperReference(SchemaModel):
    """One concrete place in the paper backing a group's numbers."""

    label: str = Field(min_length=1)
    quote: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    table_id: str | None = None
    metric: str | None = None
    value: float | None = None
    condition: str | None = None


class ComparisonQuestion(SchemaModel):
    """The user's question, restated so the pairing is unambiguous."""

    question: str = Field(min_length=1)
    restatement: str = Field(min_length=1)
    factor: str = Field(default="optimizer", min_length=1)
    target_metric: str = Field(min_length=1)
    expected_direction: str | None = None
    requires_mean: bool = False
    mean_basis: str | None = None


class ComparisonGroup(SchemaModel):
    key: GroupKey
    label: str = Field(min_length=1)
    optimizer: PlanParam[str]
    learning_rate: PlanParam[float]
    weight_decay: PlanParam[float] | None = None
    references: list[PaperReference] = Field(default_factory=list)
    reported_result: PaperReference | None = None


class PendingParam(SchemaModel):
    """A missing or out-of-range input that can change the ranking.

    While any pending item is unresolved the plan cannot be approved; the record
    itself is persisted so a page refresh restores exactly what the user saw.
    """

    id: str = Field(min_length=1)
    group_key: GroupKey | None = None
    field_path: str = Field(min_length=1)
    label: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    risk_tier: RiskTier = RiskTier.CRITICAL
    suggested_value: Any = None
    suggestion_source: str | None = None
    status: PendingStatus = PendingStatus.PENDING
    resolved_value: Any = None
    resolved_source: str | None = None
    decided_by: UUID | None = None
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def require_resolution_evidence(self) -> "PendingParam":
        if self.status is PendingStatus.PENDING:
            return self
        if self.resolved_value is None:
            raise ValueError("resolved pending item requires a resolved_value")
        if not self.resolved_source:
            raise ValueError("resolved pending item requires a resolved_source")
        return self

    @property
    def resolved(self) -> bool:
        return self.status is not PendingStatus.PENDING


class SupportGap(SchemaModel):
    """A concrete unsupported requirement surfaced on the upload page."""

    field: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    expectation: str = Field(min_length=1)


class SupportCheck(SchemaModel):
    ok: bool
    supported: list[str] = Field(default_factory=list)
    missing: list[SupportGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def ok_matches_gaps(self) -> "SupportCheck":
        if self.ok and self.missing:
            raise ValueError("a supported comparison cannot carry missing requirements")
        if not self.ok and not self.missing:
            raise ValueError("an unsupported comparison must name the missing requirement")
        return self


class ScalingAuthorization(SchemaModel):
    max_train_samples: int = Field(gt=0)
    max_test_samples: int = Field(gt=0)
    max_epochs: int = Field(gt=0)
    max_seconds: int = Field(gt=0)
    max_seeds: int = Field(default=3, ge=1, le=10)
    note: str = Field(min_length=1)


class ComparisonCandidate(SchemaModel):
    """A pair the system believes the paper actually reports."""

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    group_optimizers: list[str] = Field(min_length=2)
    metric: str | None = None
    references: list[PaperReference] = Field(default_factory=list)
    comparable: bool = True
    note: str | None = None
    blocking: list[str] = Field(default_factory=list)


class CandidateResponse(SchemaModel):
    candidates: list[ComparisonCandidate] = Field(default_factory=list)
    support: SupportCheck
    paper_references: list[PaperReference] = Field(default_factory=list)


class ComparisonPlan(SchemaModel):
    """Versioned, approvable description of one controlled comparison.

    ``base_plan`` carries every shared condition (dataset, model, loss, batch
    size, epochs, preprocessing, budget) with its existing provenance model; the
    two groups override only optimizer-specific parameters.
    """

    question: ComparisonQuestion
    base_plan: ExperimentPlan
    groups: list[ComparisonGroup] = Field(min_length=2, max_length=2)
    seeds: list[int] = Field(min_length=1)
    authorization: ScalingAuthorization
    support: SupportCheck
    pending: list[PendingParam] = Field(default_factory=list)
    paper_references: list[PaperReference] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def check_groups(self) -> "ComparisonPlan":
        keys = {g.key for g in self.groups}
        if keys != {GroupKey.A, GroupKey.B}:
            raise ValueError("comparison requires exactly one group a and one group b")
        optimizers = {g.optimizer.value for g in self.groups}
        if len(optimizers) != 2:
            raise ValueError("the two groups must use different optimizers")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique")
        if len(self.seeds) > self.authorization.max_seeds:
            raise ValueError("seeds exceed the authorized maximum")
        pending_ids = [item.id for item in self.pending]
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("pending items must have unique ids")
        return self

    def group(self, key: GroupKey) -> ComparisonGroup:
        return next(g for g in self.groups if g.key is key)

    @property
    def open_pending(self) -> list[PendingParam]:
        return [p for p in self.pending if not p.resolved]

    def approval_ready(self) -> bool:
        return self.support.ok and not self.open_pending

    def planned_pairs(self) -> int:
        return len(self.seeds)


class ComparisonPlanVersion(SchemaModel):
    id: UUID = Field(default_factory=uuid4)
    comparison_plan_id: UUID
    version: int = Field(ge=1)
    origin: PlanVersionOrigin
    parent_version_id: UUID | None = None
    plan: ComparisonPlan
    change_reason: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PendingDecision(SchemaModel):
    """User decision on one pending item (adopt suggestion or supply evidence)."""

    pending_id: str = Field(min_length=1)
    value: Any = None
    source: str | None = None
    adopt_suggestion: bool = False

    @model_validator(mode="after")
    def require_value_or_suggestion(self) -> "PendingDecision":
        if not self.adopt_suggestion and self.value is None:
            raise ValueError("provide a value or adopt the suggested one")
        if not self.adopt_suggestion and not self.source:
            raise ValueError("a user-supplied value must name its source")
        return self


class ComparisonRerunRequest(SchemaModel):
    """Rerun an approved comparison, optionally against the current environment."""

    use_current_environment: bool = False
    reason: str = Field(min_length=1)


class ReproducibilityFingerprint(SchemaModel):
    """What each group must prove it shared with its partner."""

    dataset_split_sha256: str | None = None
    init_weights_sha256: str | None = None
    sample_order_sha256: str | None = None
    template_sha256: str | None = None
    seed: int | None = None
    batch_size: int | None = None
    epochs: int | None = None
    train_subset_size: int | None = None
    test_subset_size: int | None = None
    loss_function: str | None = None
    optimizer: str | None = None
    learning_rate: float | None = None
    code_hash: str | None = None
    image_digest: str | None = None
    dataset_fingerprint: str | None = None


class RunObservation(SchemaModel):
    run_id: UUID
    group_key: GroupKey
    seed: int
    status: RunStatus
    attempt: int = Field(default=1, ge=1)
    accuracy: float | None = Field(default=None, ge=0, le=1)
    correct_count: int | None = Field(default=None, ge=0)
    evaluated_count: int | None = Field(default=None, gt=0)
    metric_value: float | None = None
    test_loss: float | None = None
    fingerprint: ReproducibilityFingerprint = Field(default_factory=ReproducibilityFingerprint)
    audit: RunAudit | None = None
    error: str | None = None


class PairCheckItem(SchemaModel):
    item: PairItem
    status: CheckStatus
    group_a: str | None = None
    group_b: str | None = None
    note: str = Field(min_length=1)


class PairVerification(SchemaModel):
    seed: int
    verified: bool
    checks: list[PairCheckItem] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def verified_requires_no_blockers(self) -> "PairVerification":
        if self.verified and self.blocking:
            raise ValueError("a verified pair cannot have blocking issues")
        return self


class SeedComparison(SchemaModel):
    """One seed's pair, with accuracy recomputed from raw counts."""

    seed: int
    group_a: RunObservation | None = None
    group_b: RunObservation | None = None
    recomputed_accuracy_a: float | None = Field(default=None, ge=0, le=1)
    recomputed_accuracy_b: float | None = Field(default=None, ge=0, le=1)
    delta: float | None = None
    delta_percentage_points: float | None = None
    direction: str | None = None
    verification: PairVerification | None = None
    complete: bool = False
    blockers: list[str] = Field(default_factory=list)


class ComparisonOutcome(SchemaModel):
    status: ComparisonStatus
    target_metric: str
    seeds: list[SeedComparison] = Field(default_factory=list)
    mean_a: float | None = None
    mean_b: float | None = None
    mean_delta: float | None = None
    mean_delta_percentage_points: float | None = None
    direction: str | None = None
    completed_pairs: int = Field(default=0, ge=0)
    requested_pairs: int = Field(default=1, ge=1)
    blockers: list[str] = Field(default_factory=list)
    verifications: list[PairVerification] = Field(default_factory=list)


class ComparisonReferences(SchemaModel):
    comparison_id: UUID
    plan_id: UUID
    plan_version_id: UUID
    plan_version: int = Field(ge=1)
    paper_id: UUID
    spec_version_id: UUID
    spec_version: int = Field(ge=1)
    run_ids: list[UUID] = Field(default_factory=list)
    code_hashes: list[str] = Field(default_factory=list)
    image_digest: str | None = None


class ComparisonReport(SchemaModel):
    references: ComparisonReferences
    question: ComparisonQuestion
    groups: list[ComparisonGroup]
    base_plan: ExperimentPlan
    seeds: list[int]
    outcome: ComparisonOutcome
    pending_decisions: list[PendingParam] = Field(default_factory=list)
    pair_verifications: list[PairVerification] = Field(default_factory=list)
    paper_references: list[PaperReference] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    final_status: ComparisonStatus
    summary: str = Field(min_length=1)
    parameter_sources: list[EvidenceRef] = Field(default_factory=list)
    scaling_note: str = Field(min_length=1)


class CandidateRequest(SchemaModel):
    card: ReproductionCard
    document: ParsedDocument | None = None
    question: str | None = None


# --- agent request/response contracts -------------------------------------


class ComparisonPlanRequest(SchemaModel):
    card: ReproductionCard
    question: str
    group_specs: list["ComparisonGroupSpec"] = Field(min_length=2, max_length=2)
    seeds: list[int] | None = None
    train_subset_size: int | None = Field(default=None, gt=0)
    test_subset_size: int | None = Field(default=None, gt=0)
    epochs: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    max_seconds: int | None = Field(default=None, gt=0)


class ComparisonGroupSpec(SchemaModel):
    key: GroupKey
    label: str = Field(min_length=1)
    optimizer: str = Field(min_length=1)
    learning_rate: float | None = Field(default=None, gt=0)
    weight_decay: float | None = Field(default=None, ge=0)
    references: list[PaperReference] = Field(default_factory=list)
    reported_result: PaperReference | None = None


class ComparisonPlanResponse(SchemaModel):
    plan: ComparisonPlan
    support: SupportCheck


class VerifyPairRequest(SchemaModel):
    plan: ComparisonPlan
    observations: list[RunObservation] = Field(default_factory=list)
    requested_pairs: int = Field(default=1, ge=1)


class VerifyPairResponse(SchemaModel):
    outcome: ComparisonOutcome


class ComparisonReportRequest(SchemaModel):
    references: ComparisonReferences
    plan: ComparisonPlan
    outcome: ComparisonOutcome
    paper_title: str | None = None
    review_notes: list[str] = Field(default_factory=list)


class ComparisonReportResponse(SchemaModel):
    report: ComparisonReport
    markdown: str = Field(min_length=1)


CandidateRequest.model_rebuild()
ComparisonPlanRequest.model_rebuild()
