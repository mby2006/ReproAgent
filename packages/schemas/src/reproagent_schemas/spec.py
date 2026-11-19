"""Structured reproduction specification extracted from the paper, with evidence."""

from pydantic import Field

from .common import SchemaModel


class Evidence(SchemaModel):
    """A verbatim quote from the paper backing a spec field."""

    quote: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None


class ModelSpec(SchemaModel):
    architecture: str = Field(min_length=1, description="e.g. 'lenet-5'")
    input_shape: list[int] = Field(min_length=1, description="e.g. [1, 28, 28]")
    num_classes: int = Field(gt=0)
    hyperparameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class DatasetSpec(SchemaModel):
    name: str = Field(min_length=1, description="e.g. 'mnist'")
    num_classes: int = Field(gt=0)
    train_size: int | None = Field(default=None, gt=0)
    test_size: int | None = Field(default=None, gt=0)
    preprocessing: dict[str, str | int | float | bool] = Field(default_factory=dict)


class TrainingSpec(SchemaModel):
    optimizer: str = Field(min_length=1)
    learning_rate: float = Field(gt=0)
    batch_size: int = Field(gt=0)
    epochs: int = Field(gt=0)
    loss: str = Field(min_length=1)
    seed: int | None = None


class EvaluationSpec(SchemaModel):
    metrics: list[str] = Field(min_length=1)
    reported_results: dict[str, float] = Field(default_factory=dict)


class ReproductionSpec(SchemaModel):
    method_summary: str = Field(min_length=1)
    model: ModelSpec
    dataset: DatasetSpec
    training: TrainingSpec
    evaluation: EvaluationSpec
    evidence: list[Evidence] = Field(min_length=1)
