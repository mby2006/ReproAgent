"""Reproduction Card: human-facing structured understanding of a paper."""

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from .common import ReviewDecision, ReviewMode, SchemaModel, check_delegation
from .evidence import EvidenceField


class ReportedResult(EvidenceField[float]):
    metric: str = Field(min_length=1)
    condition: str | None = None


class Baseline(EvidenceField[str]):
    name: str = Field(min_length=1)


class ReproductionCard(SchemaModel):
    review_mode: ReviewMode = ReviewMode.ASSISTED
    delegated_at: datetime | None = None
    delegated_by: UUID | None = None
    review_decisions: list[ReviewDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_authorization(self) -> "ReproductionCard":
        check_delegation(self.review_mode, self.delegated_at, self.delegated_by)
        return self

    review_selection_threshold: float | None = Field(default=None, ge=0, le=1)
    method: EvidenceField[str]
    model_architecture: EvidenceField[str]
    dataset: EvidenceField[str]
    data_preprocessing: dict[str, EvidenceField[str]] = Field(default_factory=dict)
    hyperparameters: dict[str, EvidenceField[str | int | float | bool]] = Field(
        default_factory=dict
    )
    optimizer: EvidenceField[str]
    loss_function: EvidenceField[str]
    evaluation_metrics: list[EvidenceField[str]] = Field(default_factory=list)
    reported_results: list[ReportedResult] = Field(default_factory=list)
    baselines: list[Baseline] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
