"""Deterministic PDF parse result. Page numbers here come from the parser, never the LLM."""

from pydantic import Field

from .common import SchemaModel


class TextBlock(SchemaModel):
    id: str = Field(min_length=1, description="stable block id, e.g. 'p3-b12'")
    text: str = Field(min_length=1)
    page: int = Field(ge=1, description="1-based physical page number")
    section: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)


class TableData(SchemaModel):
    id: str = Field(min_length=1)
    page: int = Field(ge=1)
    section: str | None = None
    caption: str | None = None
    rows: list[list[str]] = Field(default_factory=list)


class ParsedDocument(SchemaModel):
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    num_pages: int = Field(ge=1)
    blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[TableData] = Field(default_factory=list)
    ocr_required_pages: list[int] = Field(default_factory=list)
