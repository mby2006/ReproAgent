from uuid import uuid4

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    Baseline,
    CardEditRequest,
    CardVersion,
    EvidenceField,
    EvidenceStatus,
    FieldEdit,
    GroundingStatus,
    ParsedDocument,
    ReportedResult,
    ReproductionCard,
    SpecOrigin,
    TextBlock,
    UnderstandRequest,
)

PAPER_ID = uuid4()
USER_ID = uuid4()


def explicit(value, quote="some quote", page=3, **kw) -> EvidenceField:
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=quote,
        source_page=page,
        **kw,
    )


def make_card() -> ReproductionCard:
    return ReproductionCard(
        method=explicit("LeNet-5 convolutional network"),
        model_architecture=explicit("2 conv layers + 3 fully connected"),
        dataset=explicit("MNIST"),
        hyperparameters={"learning_rate": explicit(0.001, page=8)},
        optimizer=EvidenceField(value="SGD", status=EvidenceStatus.INFERRED, confidence=0.5),
        loss_function=EvidenceField(status=EvidenceStatus.MISSING),
        reported_results=[
            ReportedResult(
                metric="error_rate",
                value=0.8,
                condition="MNIST test set",
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="error rate of 0.8%",
                source_page=21,
            )
        ],
        baselines=[
            Baseline(name="fully connected net", value="baseline", status=EvidenceStatus.INFERRED)
        ],
        missing_information=["batch size not stated"],
    )


class TestEvidenceField:
    def test_explicit_requires_quote_and_page(self):
        with pytest.raises(ValidationError):
            EvidenceField(value="x", status=EvidenceStatus.PAPER_EXPLICIT)
        with pytest.raises(ValidationError):
            EvidenceField(value="x", status=EvidenceStatus.PAPER_EXPLICIT, evidence_quote="q")

    def test_paper_described_requires_quote_and_page(self):
        ok = EvidenceField(
            value="centered",
            status=EvidenceStatus.PAPER_DESCRIBED,
            evidence_quote="images are centered",
            source_page=4,
        )
        assert ok.status is EvidenceStatus.PAPER_DESCRIBED
        with pytest.raises(ValidationError):
            EvidenceField(value="centered", status=EvidenceStatus.PAPER_DESCRIBED)
        with pytest.raises(ValidationError):
            EvidenceField(
                value="centered",
                status=EvidenceStatus.PAPER_DESCRIBED,
                evidence_quote="images are centered",
            )

    def test_missing_must_not_carry_value_or_evidence(self):
        with pytest.raises(ValidationError):
            EvidenceField(value="x", status=EvidenceStatus.MISSING)
        with pytest.raises(ValidationError):
            EvidenceField(status=EvidenceStatus.MISSING, evidence_quote="q")
        ok = EvidenceField(status=EvidenceStatus.MISSING)
        assert ok.value is None

    def test_inferred_requires_value_but_not_quote(self):
        ok = EvidenceField(value="sgd", status=EvidenceStatus.INFERRED, confidence=0.4)
        assert ok.evidence_quote is None
        with pytest.raises(ValidationError):
            EvidenceField(status=EvidenceStatus.INFERRED)

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            EvidenceField(value="x", status=EvidenceStatus.INFERRED, confidence=1.5)

    def test_blank_quote_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceField(value="x", status=EvidenceStatus.INFERRED, evidence_quote="   ")

    def test_page_must_be_positive(self):
        with pytest.raises(ValidationError):
            explicit("x", page=0)

    def test_generic_value_types(self):
        assert explicit(0.001).value == 0.001
        assert explicit([1, 28, 28]).value == [1, 28, 28]


class TestGroundingStatus:
    def test_grounding_status_round_trip(self):
        field = EvidenceField(
            value="sgd",
            status=EvidenceStatus.INFERRED,
            grounding_status=GroundingStatus.LOCATED_UNSUPPORTED,
            candidate_quote="Layer C3 has 16 feature maps.",
            candidate_page=5,
            grounding_reason="引用说明 C3 层结构，不支持 optimizer=sgd",
        )
        assert EvidenceField.model_validate_json(field.model_dump_json()) == field

    def test_rejected_candidate_is_not_accepted_evidence(self):
        field = EvidenceField(
            value="sgd",
            status=EvidenceStatus.INFERRED,
            grounding_status=GroundingStatus.LOCATED_UNSUPPORTED,
            candidate_quote="Layer C3 has 16 feature maps.",
            candidate_page=5,
            grounding_reason="引用说明 C3 层结构，不支持字段值",
        )
        assert field.source_page is None
        assert field.source_section is None
        assert field.evidence_quote is None

    def test_default_grounding_status_is_not_checked(self):
        assert explicit("x").grounding_status is GroundingStatus.NOT_CHECKED

    def test_verified_requires_paper_status(self):
        with pytest.raises(ValidationError):
            EvidenceField(
                value="sgd",
                status=EvidenceStatus.INFERRED,
                evidence_quote="a quote",
                source_page=2,
                grounding_status=GroundingStatus.VERIFIED,
            )

    def test_verified_ok_with_paper_explicit(self):
        field = EvidenceField(
            value="sgd",
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote="a quote",
            source_page=2,
            grounding_status=GroundingStatus.VERIFIED,
        )
        assert field.grounding_status is GroundingStatus.VERIFIED

    def test_rejected_status_forbids_accepted_citation(self):
        with pytest.raises(ValidationError):
            EvidenceField(
                value="sgd",
                status=EvidenceStatus.INFERRED,
                evidence_quote="a quote",
                grounding_status=GroundingStatus.CITATION_NOT_FOUND,
                grounding_reason="无法定位",
            )
        with pytest.raises(ValidationError):
            EvidenceField(
                value="sgd",
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="a quote",
                source_page=2,
                grounding_status=GroundingStatus.LOCATED_UNSUPPORTED,
                grounding_reason="语义不支持",
            )

    def test_rejected_status_requires_grounding_reason(self):
        with pytest.raises(ValidationError):
            EvidenceField(
                value="sgd",
                status=EvidenceStatus.INFERRED,
                grounding_status=GroundingStatus.CITATION_NOT_FOUND,
                candidate_quote="x",
                candidate_page=2,
            )

    def test_not_checked_must_not_carry_grounding_reason(self):
        with pytest.raises(ValidationError):
            EvidenceField(
                value="sgd",
                status=EvidenceStatus.INFERRED,
                grounding_reason="stale reason without a check",
            )

    def test_missing_rejects_candidate_fields(self):
        with pytest.raises(ValidationError):
            EvidenceField(
                status=EvidenceStatus.MISSING,
                candidate_quote="Layer C3 has 16 feature maps.",
                candidate_page=5,
            )

    def test_human_confirmation_does_not_upgrade_rejected_grounding(self):
        field = EvidenceField(
            value="sgd",
            status=EvidenceStatus.INFERRED,
            grounding_status=GroundingStatus.LOCATED_UNSUPPORTED,
            candidate_quote="Layer C3 has 16 feature maps.",
            candidate_page=5,
            grounding_reason="引用仅描述层结构",
            human_confirmed=True,
        )
        assert field.grounding_status is GroundingStatus.LOCATED_UNSUPPORTED
        assert field.human_confirmed is True


class TestReproductionCard:
    def test_valid_card_roundtrip(self):
        card = make_card()
        dumped = card.model_dump(mode="json")
        restored = ReproductionCard.model_validate(dumped)
        assert restored.reported_results[0].source_page == 21
        assert restored.hyperparameters["learning_rate"].value == 0.001

    def test_card_requires_core_fields(self):
        with pytest.raises(ValidationError):
            ReproductionCard()

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ReproductionCard(**{**make_card().model_dump(), "secret": 1})


class TestParsedDocument:
    def test_block_page_positive(self):
        with pytest.raises(ValidationError):
            TextBlock(id="p0-b1", text="x", page=0)

    def test_document_defaults(self):
        doc = ParsedDocument(num_pages=43)
        assert doc.blocks == []
        assert doc.ocr_required_pages == []


class TestVersioning:
    def test_card_version(self):
        v = CardVersion(
            paper_id=PAPER_ID,
            version=1,
            origin=SpecOrigin.EXTRACTION,
            card=make_card(),
        )
        assert v.parent_version_id is None

    def test_version_must_be_positive(self):
        with pytest.raises(ValidationError):
            CardVersion(
                paper_id=PAPER_ID,
                version=0,
                origin=SpecOrigin.EXTRACTION,
                card=make_card(),
            )

    def test_field_edit_path_pattern(self):
        FieldEdit(field_path="hyperparameters.learning_rate", value=0.01)
        FieldEdit(field_path="optimizer", value=None)
        with pytest.raises(ValidationError):
            FieldEdit(field_path="reported_results[0].value", value=1)
        with pytest.raises(ValidationError):
            FieldEdit(field_path="../etc", value="x")

    def test_edit_request_requires_edits(self):
        with pytest.raises(ValidationError):
            CardEditRequest(base_version=1, edits=[])


class TestUnderstandContract:
    def test_request_requires_pdf(self):
        with pytest.raises(ValidationError):
            UnderstandRequest(paper_id=PAPER_ID, pdf_b64="")

    def test_request_valid(self):
        req = UnderstandRequest(paper_id=PAPER_ID, pdf_b64="aGVsbG8=")
        assert req.title_hint is None
