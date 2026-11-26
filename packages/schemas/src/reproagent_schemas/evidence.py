"""Evidence-backed field wrapper: every important card value carries provenance."""

from enum import StrEnum

from pydantic import Field, model_validator

from .common import ConfirmationSource, SchemaModel, check_confirmation


class EvidenceStatus(StrEnum):
    PAPER_EXPLICIT = "paper_explicit"
    PAPER_DESCRIBED = "paper_described"
    INFERRED = "inferred"
    MISSING = "missing"


class GroundingStatus(StrEnum):
    """Orthogonal to EvidenceStatus: locating a quote is not the same as support.

    A quote found on a page only proves the text exists (location). VERIFIED
    additionally requires that the quote semantically supports the field value.
    Rejected states keep the candidate quote for human review only; it never
    becomes accepted paper evidence (evidence_quote/source_page/source_section
    must stay empty), and human confirmation of a value never upgrades it.
    """

    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    CITATION_NOT_FOUND = "citation_not_found"
    LOCATED_UNSUPPORTED = "located_unsupported"


_REJECTED_GROUNDING = (
    GroundingStatus.CITATION_NOT_FOUND,
    GroundingStatus.LOCATED_UNSUPPORTED,
)


class EvidenceField[T](SchemaModel):
    value: T | None = None
    status: EvidenceStatus
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    evidence_quote: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    human_confirmed: bool = False
    confirmed_by: ConfirmationSource | None = None
    grounding_status: GroundingStatus = GroundingStatus.NOT_CHECKED
    candidate_quote: str | None = None
    candidate_page: int | None = Field(default=None, ge=1)
    candidate_section: str | None = None
    grounding_reason: str | None = None

    @model_validator(mode="after")
    def check_evidence_consistency(self) -> "EvidenceField[T]":
        check_confirmation(self.confirmed_by, self.human_confirmed)
        if self.confirmed_by is ConfirmationSource.AUTO and self.status not in (
            EvidenceStatus.INFERRED,
            EvidenceStatus.MISSING,
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
            if any(
                v is not None
                for v in (
                    self.evidence_quote,
                    self.source_page,
                    self.source_section,
                    self.candidate_quote,
                    self.candidate_page,
                    self.candidate_section,
                )
            ):
                raise ValueError("missing field must not carry evidence or candidates")
        elif self.value is None:
            raise ValueError(f"{self.status} field requires a value")
        if self.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED):
            if not self.evidence_quote:
                raise ValueError(f"{self.status.value} field requires evidence_quote")
            if self.source_page is None:
                raise ValueError(f"{self.status.value} field requires source_page")
        if self.evidence_quote is not None and not self.evidence_quote.strip():
            raise ValueError("evidence_quote must not be blank")
        if self.candidate_quote is not None and not self.candidate_quote.strip():
            raise ValueError("candidate_quote must not be blank")
        self._check_grounding()
        return self

    def _check_grounding(self) -> None:
        paper_claim = self.status in (
            EvidenceStatus.PAPER_EXPLICIT,
            EvidenceStatus.PAPER_DESCRIBED,
        )
        if self.grounding_status is GroundingStatus.VERIFIED:
            if not paper_claim:
                raise ValueError("verified grounding requires a paper-based evidence status")
        elif self.grounding_status in _REJECTED_GROUNDING:
            if paper_claim:
                raise ValueError("rejected grounding must not carry accepted paper evidence status")
            if any(
                v is not None
                for v in (
                    self.evidence_quote,
                    self.source_page,
                    self.source_section,
                )
            ):
                raise ValueError("rejected grounding must not carry an accepted citation")
            if not self.grounding_reason or not self.grounding_reason.strip():
                raise ValueError("rejected grounding requires a grounding_reason")
        else:  # NOT_CHECKED carries no grounding verdict yet
            if self.grounding_reason:
                raise ValueError("unchecked grounding must not carry a grounding_reason")
