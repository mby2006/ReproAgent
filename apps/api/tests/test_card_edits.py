from uuid import uuid4

import pytest
from reproagent_api.services import EditError, apply_edits, delegate_card
from reproagent_schemas import (
    Baseline,
    CardEditRequest,
    ConfirmationSource,
    EvidenceField,
    EvidenceStatus,
    FieldEdit,
    GroundingStatus,
    ReportedResult,
    ReproductionCard,
    ReviewMode,
)


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


def grounded_optimizer_card(
    *, value="sgd", quote="We use SGD.", page=5, section=None,
) -> ReproductionCard:
    """Card whose optimizer carries an accepted, VERIFIED paper citation."""
    return ReproductionCard(
        method=EvidenceField(value="LeNet-5", status=EvidenceStatus.INFERRED),
        model_architecture=EvidenceField(value="conv net", status=EvidenceStatus.INFERRED),
        dataset=EvidenceField(value="MNIST", status=EvidenceStatus.INFERRED),
        optimizer=EvidenceField(
            value=value,
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote=quote,
            source_page=page,
            source_section=section,
            confidence=0.9,
            grounding_status=GroundingStatus.VERIFIED,
        ),
        loss_function=EvidenceField(status=EvidenceStatus.MISSING),
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
        # Changing a paper value revokes the stale accepted citation: it must not
        # remain an accepted quote for the new value.
        assert result.optimizer.evidence_quote is None
        assert result.optimizer.source_page is None
        assert result.optimizer.status is EvidenceStatus.INFERRED
        assert result.optimizer.grounding_status is GroundingStatus.LOCATED_UNSUPPORTED
        assert result.optimizer.candidate_quote == "we use stochastic gradient descent"
        assert result.optimizer.candidate_page == 5

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


def test_card_delegation_preserves_every_value_and_evidence_across_containers():
    card = make_card()
    card.data_preprocessing["normalize"] = EvidenceField(
        value="normalize inputs", status="inferred", evidence_quote="context", source_page=2,
    )
    card.evaluation_metrics = [EvidenceField(value="accuracy", status="inferred")]
    card.reported_results = [ReportedResult(metric="accuracy", status="missing")]
    card.baselines = [Baseline(name="baseline", value="LeNet", status="inferred")]
    card.hyperparameters["seed"] = EvidenceField(
        value=42, status="inferred", human_confirmed=True, confirmed_by="human",
    )
    before = card.model_dump(mode="json")
    actor = uuid4()
    result = delegate_card(card, actor=actor)
    assert card.model_dump(mode="json") == before
    assert result.review_mode is ReviewMode.DELEGATED
    assert result.delegated_by == actor and result.delegated_at.tzinfo is not None
    assert result.method == card.method  # Explicit fields are not relabelled.
    assert result.hyperparameters["seed"] == card.hyperparameters["seed"]
    expected = {"loss_function", "hyperparameters.learning_rate", "data_preprocessing.normalize",
                "evaluation_metrics.0", "reported_results.0", "baselines.0"}
    assert {r.field_path for r in result.review_decisions[1:]} == expected
    fields = [result.loss_function, result.hyperparameters["learning_rate"],
              result.data_preprocessing["normalize"], result.evaluation_metrics[0],
              result.reported_results[0], result.baselines[0]]
    for field in fields:
        assert field.confirmed_by is ConfirmationSource.AUTO and not field.human_confirmed
    assert result.loss_function.status is EvidenceStatus.MISSING
    assert result.loss_function.value is None and result.loss_function.evidence_quote is None
    assert result.reported_results[0].value is None
    for record in result.review_decisions:
        assert record.actor == actor and record.timestamp == result.delegated_at
        assert record.review_mode is ReviewMode.DELEGATED
        if record.field_path != "review_mode":
            assert record.before == record.after
            assert record.confirmed_by is ConfirmationSource.AUTO
            assert record.recommendation_id is None  # No fabricated recommendation.
    assert result.data_preprocessing["normalize"].evidence_quote == "context"
    assert result.data_preprocessing["normalize"].source_page == 2
    assert result.hyperparameters["learning_rate"].confidence == 0.5
    assert ReproductionCard.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("mode", [None, "expert", "assisted"])
@pytest.mark.parametrize("source", [ConfirmationSource.HUMAN, ConfirmationSource.AI_ASSISTED])
def test_edit_revokes_card_authorization_and_appends_audit_without_mutation(mode, source):
    actor = uuid4()
    original = delegate_card(make_card(), actor=actor)
    before = original.model_dump(mode="json")
    request = edits(("hyperparameters.learning_rate", 0.001))
    request.review_mode = ReviewMode(mode) if mode else None
    result = apply_edits(original, request, actor=actor, confirmed_by=source)
    assert original.model_dump(mode="json") == before
    assert result.review_mode is ReviewMode(mode or "assisted")
    assert result.delegated_at is None and result.delegated_by is None
    assert result.loss_function.confirmed_by is ConfirmationSource.AUTO
    assert not result.loss_function.human_confirmed
    field = result.hyperparameters["learning_rate"]
    assert field.human_confirmed and field.confirmed_by is source
    assert field.status is EvidenceStatus.INFERRED
    assert result.review_decisions[:-2] == original.review_decisions
    assert result.review_decisions[-2].field_path == "review_mode"
    decision = result.review_decisions[-1]
    assert decision.field_path == "hyperparameters.learning_rate"
    assert decision.before == decision.after == 0.001
    assert decision.actor == actor and decision.confirmed_by is source


def test_card_delegation_requires_actor_and_cannot_be_impersonated():
    card = delegate_card(make_card(), actor=uuid4())
    with pytest.raises(EditError, match="another actor"):
        delegate_card(card, actor=uuid4())
    with pytest.raises(EditError, match="actor"):
        delegate_card(make_card(), actor=None)
    with pytest.raises(EditError, match="delegated review"):
        apply_edits(make_card(), edits(("optimizer", "sgd")), confirmed_by=ConfirmationSource.AUTO)


class TestCitationRevocation:
    """Editing a paper value must revoke the stale citation but keep the candidate."""

    def test_changing_paper_value_revokes_old_citation(self):
        card = grounded_optimizer_card(value="sgd", quote="We use SGD.", page=5)
        request = CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ])
        result = apply_edits(card, request)
        assert result.optimizer.value == "adam"
        assert result.optimizer.status is EvidenceStatus.INFERRED
        assert result.optimizer.grounding_status is GroundingStatus.LOCATED_UNSUPPORTED
        assert result.optimizer.evidence_quote is None
        assert result.optimizer.source_page is None
        assert result.optimizer.source_section is None
        assert result.optimizer.candidate_quote == "We use SGD."
        assert result.optimizer.candidate_page == 5
        # human_confirmed records who made the edit; it never upgrades grounding.
        assert result.optimizer.human_confirmed is True
        assert result.optimizer.grounding_reason == "字段值已改变，原引用不能作为新值的论文证据"

    def test_same_value_keeps_accepted_citation_and_verified_grounding(self):
        card = grounded_optimizer_card(value="sgd", quote="We use SGD.", page=5)
        request = CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="sgd", human_confirmed=True)
        ])
        result = apply_edits(card, request)
        assert result.optimizer.status is EvidenceStatus.PAPER_EXPLICIT
        assert result.optimizer.grounding_status is GroundingStatus.VERIFIED
        assert result.optimizer.evidence_quote == "We use SGD."
        assert result.optimizer.source_page == 5
        assert result.optimizer.candidate_quote is None
        assert result.optimizer.human_confirmed is True

    def test_human_confirmation_does_not_upgrade_to_verified(self):
        # human_confirmed=True must not turn a revoked citation back into VERIFIED.
        card = grounded_optimizer_card()
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ]))
        assert result.optimizer.human_confirmed is True
        assert result.optimizer.grounding_status is not GroundingStatus.VERIFIED
        assert result.optimizer.grounding_status is GroundingStatus.LOCATED_UNSUPPORTED

    def test_editing_rejected_candidate_retains_candidate_and_stays_unverified(self):
        card = ReproductionCard(
            method=EvidenceField(value="LeNet-5", status=EvidenceStatus.INFERRED),
            model_architecture=EvidenceField(value="conv net", status=EvidenceStatus.INFERRED),
            dataset=EvidenceField(value="MNIST", status=EvidenceStatus.INFERRED),
            loss_function=EvidenceField(status=EvidenceStatus.MISSING),
            optimizer=EvidenceField(
                value="sgd",
                status=EvidenceStatus.INFERRED,
                grounding_status=GroundingStatus.LOCATED_UNSUPPORTED,
                candidate_quote="Layer C3 has 16 feature maps.",
                candidate_page=5,
                grounding_reason="引用说明 C3 层结构，不支持 optimizer=sgd",
            ),
        )
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ]))
        assert result.optimizer.value == "adam"
        assert result.optimizer.status is EvidenceStatus.INFERRED
        assert result.optimizer.grounding_status is not GroundingStatus.VERIFIED
        # The prior rejected candidate is retained as review context, never lost.
        assert result.optimizer.candidate_quote == "Layer C3 has 16 feature maps."
        assert result.optimizer.candidate_page == 5

    def test_clearing_value_marks_missing_and_drops_candidates(self):
        card = grounded_optimizer_card(value="sgd", quote="We use SGD.", page=5)
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value=None, human_confirmed=True)
        ]))
        assert result.optimizer.status is EvidenceStatus.MISSING
        assert result.optimizer.value is None
        assert result.optimizer.evidence_quote is None
        assert result.optimizer.source_page is None
        assert result.optimizer.candidate_quote is None
        assert result.optimizer.grounding_status is GroundingStatus.NOT_CHECKED

    def test_value_with_no_prior_citation_generates_no_candidate(self):
        # loss_function is MISSING (no citation). Filling it must not fabricate a
        # candidate; the field stays inferred and unchecked.
        card = make_card()
        result = apply_edits(card, edits(("loss_function", "cross entropy")))
        assert result.loss_function.status is EvidenceStatus.INFERRED
        assert result.loss_function.grounding_status is GroundingStatus.NOT_CHECKED
        assert result.loss_function.candidate_quote is None
        assert result.loss_function.grounding_reason is None

    def test_edit_records_previous_evidence_and_grounding_reason(self):
        card = grounded_optimizer_card(value="sgd", quote="We use SGD.", page=5,
                                       section="3.1 Training")
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ]))
        decision = result.review_decisions[-1]
        assert decision.field_path == "optimizer"
        assert decision.before == "sgd" and decision.after == "adam"
        assert decision.grounding_reason == "字段值已改变，原引用不能作为新值的论文证据"
        assert decision.previous_evidence == {
            "status": "paper_explicit",
            "grounding_status": "verified",
            "value": "sgd",
            "evidence_quote": "We use SGD.",
            "source_page": 5,
            "source_section": "3.1 Training",
        }

    def test_same_value_edit_records_no_revocation_audit(self):
        card = grounded_optimizer_card()
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="sgd", human_confirmed=True)
        ]))
        decision = result.review_decisions[-1]
        assert decision.previous_evidence is None
        assert decision.grounding_reason is None

    def test_no_revocable_citation_records_audit_note(self):
        card = make_card()
        result = apply_edits(card, edits(("loss_function", "cross entropy")))
        decision = result.review_decisions[-1]
        assert decision.previous_evidence["status"] == "missing"
        assert decision.previous_evidence["grounding_status"] == "not_checked"
        assert decision.grounding_reason == "旧字段没有可撤销的已接受引用"

    def test_base_card_not_mutated_by_revocation(self):
        card = grounded_optimizer_card(value="sgd", quote="We use SGD.", page=5)
        before = card.model_dump(mode="json")
        apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ]))
        assert card.model_dump(mode="json") == before
        assert card.optimizer.grounding_status is GroundingStatus.VERIFIED
        assert card.optimizer.evidence_quote == "We use SGD."

    def test_resulting_card_round_trips(self):
        card = grounded_optimizer_card()
        result = apply_edits(card, CardEditRequest(base_version=1, edits=[
            FieldEdit(field_path="optimizer", value="adam", human_confirmed=True)
        ]))
        assert ReproductionCard.model_validate_json(result.model_dump_json()) == result

