"""Evidence-backed field wrapper: every important card value carries provenance."""

from enum import StrEnum

from pydantic import Field, model_validator

from .common import ConfirmationSource, SchemaModel, check_confirmation


class EvidenceStatus(StrEnum):
    PAPER_EXPLICIT = "paper_explicit"
    PAPER_DESCRIBED = "paper_described"
    INFERRED = "inferred"
    MISSING = "missing"


class EvidenceField[T](SchemaModel):
    value: T | None = None
    status: EvidenceStatus
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    evidence_quote: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    human_confirmed: bool = False
    confirmed_by: ConfirmationSource | None = None

    @model_validator(mode="after")
    def check_evidence_consistency(self) -> "EvidenceField[T]":
        check_confirmation(self.confirmed_by, self.human_confirmed)
        if self.confirmed_by is ConfirmationSource.AUTO and self.status not in (
            EvidenceStatus.INFERRED, EvidenceStatus.MISSING,
        ):
            raise ValueError("automatic acceptance must remain inferred or missing")
        if (
            self.confirmed_by is ConfirmationSource.AI_ASSISTED
            and self.status is not EvidenceStatus.INFERRED
        ):
            raise ValueError("AI-assisted acceptance must remain inferred")
        if self.status is EvidenceStatus.MISSING:
            if self.value is not None:
                raise ValueError("missing field must not carry a value")
            if any(v is not None for v in (
                self.evidence_quote, self.source_page, self.source_section,
            )):
                raise ValueError("missing field must not carry evidence")
        elif self.value is None:
            raise ValueError(f"{self.status} field requires a value")
        if self.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED):
            if not self.evidence_quote:
                raise ValueError(f"{self.status.value} field requires evidence_quote")
            if self.source_page is None:
                raise ValueError(f"{self.status.value} field requires source_page")
        if self.evidence_quote is not None and not self.evidence_quote.strip():
            raise ValueError("evidence_quote must not be blank")
        return self
