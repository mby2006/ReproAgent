import hashlib
import json
from typing import Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .card import ReproductionCard
from .common import ManualReviewMode, ReviewMode, SchemaModel
from .evidence import EvidenceStatus
from .plan import AssumptionKind, ExperimentPlan, RiskTier
from .workflow import LLMUsage

ReviewValue = StrictStr | StrictInt | StrictFloat | StrictBool | None


class ReviewField(SchemaModel):
    field_path: str
    value: ReviewValue
    risk_tier: RiskTier = RiskTier.CRITICAL
    inferred: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    auto_acceptable: bool = False


class ReviewRequest(SchemaModel):
    card: ReproductionCard
    plan: ExperimentPlan | None = None
    field_paths: list[str] = Field(default_factory=list, max_length=100)
    scope: str = Field(min_length=1)

    def context_hash(self) -> str:
        data = self.model_dump(mode="json", by_alias=True, exclude={"field_paths"})
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def review_fields(self) -> list[ReviewField]:
        if self.plan is not None:
            fields = [
                ReviewField(
                    field_path=a.field_path,
                    value=a.value,
                    risk_tier=a.risk_tier,
                    inferred=a.kind is AssumptionKind.INFERRED,
                    confidence=a.confidence,
                    auto_acceptable=a.auto_acceptable,
                )
                for a in self.plan.unconfirmed_required()
                if a.risk_tier is not RiskTier.INFO
            ]
        else:
            entries = {
                key: getattr(self.card, key)
                for key in ("method", "model_architecture", "dataset", "optimizer", "loss_function")
            }
            for container in ("hyperparameters", "data_preprocessing"):
                entries.update({
                    f"{container}.{key}": field
                    for key, field in getattr(self.card, container).items()
                })
            routine = {
                "hyperparameters.weight_decay", "hyperparameters.seed",
                "hyperparameters.batch_size", "hyperparameters.epochs",
                "data_preprocessing.normalize", "data_preprocessing.normalization",
                "data_preprocessing.to_tensor",
            }
            fields = [
                ReviewField(
                    field_path=path,
                    value=field.value,
                    risk_tier=RiskTier.ROUTINE if path in routine else RiskTier.CRITICAL,
                    inferred=field.status is EvidenceStatus.INFERRED,
                    confidence=field.confidence,
                    auto_acceptable=path in routine,
                )
                for path, field in entries.items()
                if field.status in (EvidenceStatus.INFERRED, EvidenceStatus.MISSING)
                and not field.human_confirmed
            ]
        if not self.field_paths:
            return fields
        requested = set(self.field_paths)
        if len(requested) != len(self.field_paths):
            raise ValueError("duplicate field paths")
        if requested - {field.field_path for field in fields}:
            raise ValueError("only pending review fields may be requested")
        return [field for field in fields if field.field_path in requested]


def preselected_paths(fields: list[ReviewField], threshold: float) -> list[str]:
    return [
        field.field_path for field in fields
        if field.risk_tier is RiskTier.ROUTINE and field.auto_acceptable
        and field.inferred and field.confidence >= threshold
    ]


class FieldExplanation(SchemaModel):
    field_path: str
    what: str = Field(min_length=1, max_length=4000)
    why: str = Field(min_length=1, max_length=4000)
    consequence: str = Field(min_length=1, max_length=4000)
    risk: str = Field(min_length=1, max_length=4000)


class Recommendation(SchemaModel):
    field_path: str
    action: Literal["accept", "modify"]
    suggested_value: ReviewValue
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1, max_length=4000)
    risk: str = Field(min_length=1, max_length=4000)


class FieldRecommendation(Recommendation):
    id: str = Field(min_length=1)


class ExplainResponse(SchemaModel):
    context_hash: str
    explanations: list[FieldExplanation]
    preselected_paths: list[str] = Field(default_factory=list)
    selection_threshold: float = Field(default=0.9, ge=0, le=1)
    usages: list[LLMUsage] = Field(default_factory=list)


class RecommendResponse(SchemaModel):
    context_hash: str
    recommendations: list[FieldRecommendation]
    preselected_paths: list[str] = Field(default_factory=list)
    selection_threshold: float = Field(default=0.9, ge=0, le=1)
    usages: list[LLMUsage] = Field(default_factory=list)


class ExplainFieldsRequest(SchemaModel):
    field_paths: list[str] = Field(default_factory=list, max_length=100)


class CardExplainRequest(ExplainFieldsRequest):
    base_version: int = Field(ge=1)


class AdoptRecommendationsRequest(SchemaModel):
    context_hash: str = Field(min_length=64, max_length=64)
    recommendation_ids: list[str] = Field(min_length=1, max_length=100)
    review_mode: ManualReviewMode = ReviewMode.ASSISTED

    @model_validator(mode="after")
    def distinct_recommendations(self) -> "AdoptRecommendationsRequest":
        if len(set(self.recommendation_ids)) != len(self.recommendation_ids):
            raise ValueError("duplicate recommendations")
        return self


class CardConfirmBatchRequest(CardExplainRequest):
    field_paths: list[str] = Field(min_length=1, max_length=100)
    context_hash: str = Field(min_length=64, max_length=64)
    review_mode: ManualReviewMode = ReviewMode.ASSISTED


class DelegateReviewRequest(SchemaModel):
    base_version: int = Field(ge=1)
    consent: Literal[True]

    @field_validator("consent", mode="before")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError("explicit consent=true is required")
        return value


class ReviewModeRequest(SchemaModel):
    review_mode: ManualReviewMode = ReviewMode.ASSISTED


class TierOverrideRequest(SchemaModel):
    field_path: str = Field(min_length=1)
    risk_tier: RiskTier
