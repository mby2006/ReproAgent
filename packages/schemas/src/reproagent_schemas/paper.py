from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, HttpUrl, model_validator

from .common import SchemaModel, utcnow

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_PDF_BASE64_LENGTH = 4 * ((MAX_PDF_BYTES + 2) // 3)


class PaperSource(StrEnum):
    UPLOAD = "upload"
    URL = "url"


class PaperStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    FAILED = "failed"


# Ownership is transitive: project_id -> projects.user_id (enforced by RLS).
class Paper(SchemaModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    title: str = Field(min_length=1, max_length=500)
    doi: str | None = None
    source: PaperSource = PaperSource.UPLOAD
    source_url: HttpUrl | None = None
    pdf_path: str | None = None
    file_sha256: str | None = None
    metadata_json: dict = Field(default_factory=dict)
    status: PaperStatus = PaperStatus.UPLOADED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def check_source_consistency(self) -> "Paper":
        if self.source is PaperSource.URL and self.source_url is None:
            raise ValueError("source_url is required when source is 'url'")
        if self.source is PaperSource.UPLOAD and self.pdf_path is None:
            raise ValueError("pdf_path is required when source is 'upload'")
        return self
