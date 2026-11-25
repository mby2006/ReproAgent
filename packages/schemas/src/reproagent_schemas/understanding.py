"""Contracts for the paper-understanding pipeline (api <-> agent) and card versioning."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from .card import ReproductionCard
from .common import ManualReviewMode, SchemaModel, utcnow
from .jobs import JobStatus
from .paper import MAX_PDF_BASE64_LENGTH
from .parsed import ParsedDocument
from .workflow import LLMUsage


class UnderstandRequest(SchemaModel):
    paper_id: UUID
    pdf_b64: str = Field(
        min_length=1, max_length=MAX_PDF_BASE64_LENGTH, description="base64-encoded PDF bytes"
    )
    title_hint: str | None = None


class UnderstandResponse(SchemaModel):
    paper_id: UUID
    document: ParsedDocument
    card: ReproductionCard


class ParseRequest(SchemaModel):
    pdf_b64: str = Field(
        min_length=1, max_length=MAX_PDF_BASE64_LENGTH, description="base64-encoded PDF bytes"
    )


class ParseResponse(SchemaModel):
    document: ParsedDocument


class ExtractRequest(SchemaModel):
    document: ParsedDocument
    title_hint: str | None = None


class ExtractResponse(SchemaModel):
    card_raw: dict
    usages: list[LLMUsage] = Field(default_factory=list)


class GroundRequest(SchemaModel):
    card_raw: dict
    document: ParsedDocument


class GroundResponse(SchemaModel):
    card: ReproductionCard


# Ownership is enforced in the database via papers.project_id -> projects.user_id,
# so these read models do not carry a user_id column.
class UnderstandingJob(SchemaModel):
    id: UUID = Field(default_factory=uuid4)
    paper_id: UUID
    status: JobStatus = JobStatus.QUEUED
    spec_version_id: UUID | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SpecOrigin(StrEnum):
    EXTRACTION = "extraction"
    USER_EDIT = "user_edit"


class CardVersion(SchemaModel):
    id: UUID = Field(default_factory=uuid4)
    paper_id: UUID
    version: int = Field(ge=1)
    origin: SpecOrigin
    parent_version_id: UUID | None = None
    card: ReproductionCard
    created_at: datetime = Field(default_factory=utcnow)


class FieldEdit(SchemaModel):
    """Edit one scalar field ('optimizer') or dict entry ('hyperparameters.learning_rate')."""

    field_path: str = Field(min_length=1, pattern=r"^[a-z_]+(\.[A-Za-z0-9_\-]+)?$")
    value: str | int | float | bool | None = None
    human_confirmed: bool = True


class CardEditRequest(SchemaModel):
    base_version: int = Field(ge=1)
    edits: list[FieldEdit] = Field(min_length=1)
    review_mode: ManualReviewMode | None = None
