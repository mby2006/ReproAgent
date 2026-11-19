"""API -> Agent request contract for starting a reproduction."""

from uuid import UUID

from pydantic import Field

from .common import OwnedModel


class ReproductionRequest(OwnedModel):
    paper_id: UUID
    title: str = Field(min_length=1)
    paper_text: str | None = None
    source_url: str | None = None
