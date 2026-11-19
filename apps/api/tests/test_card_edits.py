import pytest
from reproagent_api.services import EditError, apply_edits
from reproagent_schemas import CardEditRequest, EvidenceField, EvidenceStatus, ReproductionCard


def make_card() -> ReproductionCard:
    explicit = EvidenceField(
        value="sgd",
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote="we use stochastic gradient descent",
        source_page=5,
        confidence=0.9,
    )
    return ReproductionCard(
        method=explicit.model_copy(update={"value": "LeNet-5"}),
        model_architecture=explicit.model_copy(update={"value": "conv net"}),
        dataset=explicit.model_copy(update={"value": "MNIST"}),
        optimizer=explicit,
        loss_function=EvidenceField(status=EvidenceStatus.MISSING),
        hyperparameters={
            "learning_rate": EvidenceField(
                value=0.001,
                status=EvidenceStatus.INFERRED,
                confidence=0.5,
            )
        },
    )


def edits(*pairs) -> CardEditRequest:
    return CardEditRequest(
        base_version=1,
        edits=[{"field_path": path, "value": value} for path, value in pairs],
    )


class TestApplyEdits:
    def test_confirm_inferred_value(self):
        card = make_card()
        result = apply_edits(card, edits(("hyperparameters.learning_rate", 0.001)))
        field = result.hyperparameters["learning_rate"]
        assert field.human_confirmed is True
        assert field.status is EvidenceStatus.INFERRED
        assert field.value == 0.001

    def test_change_scalar_value(self):
        result = apply_edits(make_card(), edits(("optimizer", "adam")))
        assert result.optimizer.value == "adam"
        assert result.optimizer.human_confirmed is True
        assert result.optimizer.evidence_quote is not None

    def test_fill_missing_field_becomes_inferred(self):
        result = apply_edits(make_card(), edits(("loss_function", "cross entropy")))
        assert result.loss_function.status is EvidenceStatus.INFERRED
        assert result.loss_function.value == "cross entropy"
        assert result.loss_function.human_confirmed is True

    def test_mark_field_missing(self):
        result = apply_edits(make_card(), edits(("optimizer", None)))
        assert result.optimizer.status is EvidenceStatus.MISSING
        assert result.optimizer.value is None
        assert result.optimizer.evidence_quote is None

    def test_base_card_not_mutated(self):
        card = make_card()
        apply_edits(card, edits(("optimizer", "adam")))
        assert card.optimizer.value == "sgd"
        assert card.optimizer.human_confirmed is False

    def test_unknown_field_rejected(self):
        with pytest.raises(EditError):
            apply_edits(make_card(), edits(("reported_results", "x")))
        with pytest.raises(EditError):
            apply_edits(make_card(), edits(("hyperparameters.unknown_key", 1)))

    def test_invalid_value_type_rejected(self):
        with pytest.raises(EditError):
            apply_edits(make_card(), edits(("optimizer", 123)))
