"""Unit tests for the paper support gate (check_supported).

Validates that OCR-required, non-MNIST, and non-LeNet papers are rejected
before any Runner container is created.
"""

import pytest
from reproagent_agent.planning import check_supported
from reproagent_agent.planning.engine import (
    SupportCheck,
    UnsupportedPaperError,
    _guard_lenet_mnist,
)
from reproagent_schemas import (
    EvidenceField,
    EvidenceStatus,
    ParsedDocument,
    ReproductionCard,
    TextBlock,
)


def explicit(value, page=8):
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=f"...{value}...",
        source_page=page,
    )


def minimal_card(**overrides) -> ReproductionCard:
    base = dict(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross-entropy"),
        hyperparameters={"learning_rate": explicit("0.001")},
        evaluation_metrics=[explicit("accuracy")],
    )
    base.update(overrides)
    return ReproductionCard(**base)


def make_document(ocr_pages=None, blocks=None, num_pages=1):
    return ParsedDocument(
        num_pages=num_pages,
        blocks=blocks or [TextBlock(id="p1-b1", text="LeNet-5 MNIST", page=1)],
        ocr_required_pages=ocr_pages or [],
    )


class TestScannedPdf:
    def test_scanned_document_never_reaches_planning(self):
        doc = make_document(ocr_pages=[2])
        result = check_supported(doc, None)
        assert result.supported is False
        assert result.code == "SCANNED_PDF"

    def test_scanned_document_ignores_card_content(self):
        doc = make_document(ocr_pages=[1, 3])
        card = minimal_card()
        result = check_supported(doc, card)
        assert result.supported is False
        assert result.code == "SCANNED_PDF"


class TestUnsupportedDataset:
    def test_non_mnist_card_has_stable_reason(self):
        card = minimal_card(dataset=explicit("CIFAR-10 images"))
        doc = make_document()
        result = check_supported(doc, card)
        assert result.supported is False
        assert result.code == "UNSUPPORTED_DATASET"
        assert "MNIST" in result.message

    def test_missing_dataset_status_rejected(self):
        card = minimal_card(dataset=EvidenceField(status=EvidenceStatus.MISSING))
        doc = make_document()
        result = check_supported(doc, card)
        assert result.supported is False
        assert result.code == "UNSUPPORTED_DATASET"


class TestUnsupportedModel:
    def test_non_lenet_card_has_stable_reason(self):
        card = minimal_card(
            model_architecture=explicit("ResNet-50"),
            method=explicit("residual learning"),
        )
        doc = make_document()
        result = check_supported(doc, card)
        assert result.supported is False
        assert result.code == "UNSUPPORTED_MODEL"
        assert "LeNet" in result.message

    def test_lenet_in_method_is_sufficient(self):
        card = minimal_card(
            model_architecture=explicit("custom convolutional"),
            method=explicit("LeNet-5 style training"),
        )
        doc = make_document()
        result = check_supported(doc, card)
        assert result.supported is True
        assert result.code == "SUPPORTED"


class TestSupported:
    def test_lenet_mnist_is_supported(self):
        doc = make_document()
        card = minimal_card()
        result = check_supported(doc, card)
        assert result == SupportCheck(
            supported=True, code="SUPPORTED", message="LeNet-5 \u00d7 MNIST"
        )

    def test_support_check_is_frozen(self):
        result = check_supported(make_document(), minimal_card())
        with pytest.raises(AttributeError):
            result.supported = False  # type: ignore


class TestCardRequiredAfterExtraction:
    def test_none_card_raises_value_error_when_not_scanned(self):
        doc = make_document()
        with pytest.raises(ValueError, match="card is required"):
            check_supported(doc, None)


class TestErrorCodeAttribute:
    """Important #2: the gate discriminates on ``exc.code``, not message text."""

    def test_dataset_guard_error_has_code(self):
        card = minimal_card(dataset=explicit("CIFAR-10 images"))
        with pytest.raises(UnsupportedPaperError) as excinfo:
            _guard_lenet_mnist(card)
        assert excinfo.value.code == "UNSUPPORTED_DATASET"
        # message text still mentions MNIST for human readability
        assert "MNIST" in str(excinfo.value)

    def test_model_guard_error_has_code(self):
        card = minimal_card(
            model_architecture=explicit("ResNet-50"),
            method=explicit("residual learning"),
        )
        with pytest.raises(UnsupportedPaperError) as excinfo:
            _guard_lenet_mnist(card)
        assert excinfo.value.code == "UNSUPPORTED_MODEL"

    def test_dataset_code_drives_gate_not_message_text(self):
        # Prove the branch keys off ``exc.code``: monkeypatch the guard to raise
        # a dataset error whose message text would have fooled the old
        # ``"MNIST" in str(exc)`` string matcher (no "MNIST" substring present).
        import reproagent_agent.planning.engine as engine

        original = engine._guard_lenet_mnist
        try:
            def fake_guard(card):
                raise UnsupportedPaperError("dataset rejected", code="UNSUPPORTED_DATASET")

            engine._guard_lenet_mnist = fake_guard
            result = check_supported(make_document(), minimal_card())
        finally:
            engine._guard_lenet_mnist = original
        assert result.supported is False
        assert result.code == "UNSUPPORTED_DATASET"

    def test_default_code_when_not_specified(self):
        exc = UnsupportedPaperError("boom")
        assert exc.code == "UNSUPPORTED_PAPER"
