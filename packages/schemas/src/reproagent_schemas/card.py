"""Reproduction Card: human-facing structured understanding of a paper."""

from pydantic import Field

from .common import SchemaModel
from .evidence import EvidenceField


class ReportedResult(EvidenceField[float]):
    metric: str = Field(min_length=1)
    condition: str | None = None


class Baseline(EvidenceField[str]):
    name: str = Field(min_length=1)


class ReproductionCard(SchemaModel):
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
