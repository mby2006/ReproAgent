from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ReviewMode(StrEnum):
    EXPERT = "expert"
    ASSISTED = "assisted"
    DELEGATED = "delegated"


ManualReviewMode = Literal[ReviewMode.EXPERT, ReviewMode.ASSISTED]


class ConfirmationSource(StrEnum):
    HUMAN = "human"
    AI_ASSISTED = "ai_assisted"
    AUTO = "auto"


def check_confirmation(source: ConfirmationSource | None, human_confirmed: bool) -> None:
    if source is ConfirmationSource.AUTO:
        if human_confirmed:
            raise ValueError("automatic acceptance is not a human confirmation")
    elif source is not None and not human_confirmed:
        raise ValueError("confirmation provenance requires a human decision")


def check_delegation(mode: ReviewMode, at: datetime | None, by: UUID | None) -> None:
    if (at is None) != (by is None):
        raise ValueError("delegated_at and delegated_by must be paired")
    if at is not None and (at.tzinfo is None or at.utcoffset() is None):
        raise ValueError("delegated_at must be timezone aware")
    if mode is ReviewMode.DELEGATED:
        if at is None:
            raise ValueError("delegated review requires explicit authorization")
    elif at is not None:
        raise ValueError("ordinary review cannot retain delegated authorization")


def utcnow() -> datetime:
    return datetime.now(UTC)


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewDecision(SchemaModel):
    id: UUID = Field(default_factory=uuid4)
    field_path: str
    before: Any
    after: Any
    confirmed_by: ConfirmationSource | None = None
    review_mode: ReviewMode | None = None
    actor: UUID | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    recommendation_id: str | None = None
    recommendation_confidence: float | None = Field(default=None, ge=0, le=1)
    recommendation_reasoning: str | None = None
    recommendation_risk: str | None = None
    preselection_threshold: float | None = Field(default=None, ge=0, le=1)
    grounding_reason: str | None = None
    previous_evidence: dict[str, Any] | None = None


class OwnedModel(SchemaModel):
    """Base for every business object: user ownership is mandatory."""

    user_id: UUID


class IdentifiedModel(OwnedModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
