"""Shared assisted-review contracts; no agent, API, or provider dependencies."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    AdoptRecommendationsRequest,
    Assumption,
    CardConfirmBatchRequest,
    CardEditRequest,
    CardExplainRequest,
    ConfirmationSource,
    DelegateReviewRequest,
    EvidenceField,
    ExperimentPlan,
    ExplainFieldsRequest,
    ExplainResponse,
    FieldExplanation,
    FieldRecommendation,
    PlanChangeRecord,
    PlanEditRequest,
    PlanParam,
    RecommendResponse,
    ReproductionCard,
    ReviewDecision,
    ReviewField,
    ReviewMode,
    ReviewModeRequest,
    ReviewRequest,
    preselected_paths,
    utcnow,
)


def review_card():
    return ReproductionCard.model_validate(
        {
            "method": {"status": "missing"},
            "model_architecture": {"status": "missing"},
            "dataset": {
                "value": "MNIST",
                "status": "paper_explicit",
                "evidence_quote": "We evaluate on MNIST.",
                "source_page": 3,
            },
            "optimizer": {"value": "sgd", "status": "inferred", "confidence": 0.99},
            "loss_function": {"status": "missing", "confidence": 0.99},
            "hyperparameters": {
                "weight_decay": {"value": 0.0, "status": "inferred", "confidence": 0.9},
                "seed": {"value": 42, "status": "inferred", "confidence": 0.89},
                "batch_size": {"status": "missing", "confidence": 0.99},
                "epochs": {
                    "value": 3,
                    "status": "inferred",
                    "confidence": 0.99,
                    "human_confirmed": True,
                    "confirmed_by": "ai_assisted",
                },
            },
            "data_preprocessing": {
                "normalize": {"value": "unit range", "status": "inferred", "confidence": 0.95},
                "custom": {"value": "crop", "status": "inferred", "confidence": 0.99},
            },
        }
    )


def review_plan():
    def param(value):
        return {"value": value, "source": "inferred", "rationale": "Extraction inference"}

    return ExperimentPlan.model_validate(
        {
            "rationale": "Small reproducible experiment",
            "dataset_config": {"train_subset_size": 100, "test_subset_size": 20},
            "model_config": {},
            "preprocessing_config": {},
            "training_config": {
                "optimizer": param("sgd"),
                "learning_rate": param(0.01),
                "loss_function": param("cross_entropy"),
                "batch_size": param(32),
                "epochs": param(3),
                "weight_decay": param(0.0),
            },
            "evaluation_config": {
                "metrics": ["accuracy"],
                "target_metric": "accuracy",
                "tolerance": 0.03,
                "comparison_note": "Scaled, not paper-equivalent",
            },
            "runtime_budget": {},
            "expected_runtime": {"estimated_seconds": 60, "basis": "CPU estimate"},
            "resource_estimates": {},
            "assumptions": [
                {
                    "field_path": path,
                    "kind": kind,
                    "value": value,
                    "rationale": "Extraction inference",
                    "risk_tier": tier,
                    "confidence": confidence,
                    "auto_acceptable": True,
                    "human_confirmed": confirmed,
                    "requires_confirmation": required,
                }
                for path, kind, value, tier, confidence, confirmed, required in [
                    ("training_config.optimizer", "inferred", "sgd", "critical", 0.99, False, True),
                    ("training_config.weight_decay", "inferred", 0.0, "routine", 0.91, False, True),
                    ("training_config.epochs", "missing_proposal", 3, "routine", 0.99, False, True),
                    ("training_config.batch_size", "inferred", 32, "routine", 0.99, True, True),
                    ("dataset_config", "mvp_scaling", "mnist", "info", 0.99, False, True),
                    ("runtime_budget.max_seconds", "inferred", 600, "routine", 0.99, False, False),
                ]
            ],
        }
    )


def recommendation(**overrides):
    return {
        "id": "server-advice-1",
        "field_path": "training_config.optimizer",
        "action": "accept",
        "suggested_value": "sgd",
        "confidence": 0.98,
        "reasoning": "Safe baseline",
        "risk": "May differ from the paper",
        **overrides,
    }


def test_modes_and_confirmation_sources_are_explicit_and_roundtrip():
    assert {mode.value for mode in ReviewMode} == {"expert", "assisted", "delegated"}
    assert {source.value for source in ConfirmationSource} == {"human", "ai_assisted", "auto"}
    assert ReviewModeRequest().review_mode is ReviewMode.ASSISTED
    assert review_card().review_mode is ReviewMode.ASSISTED
    assert review_plan().review_mode is ReviewMode.ASSISTED
    for source in ConfirmationSource:
        field = EvidenceField(
            value="sgd",
            status="inferred",
            confidence=0.7,
            human_confirmed=source is not ConfirmationSource.AUTO,
            confirmed_by=source,
        )
        restored = EvidenceField.model_validate_json(field.model_dump_json())
        assert restored.confirmed_by is source
        assert restored.status == "inferred"
        assert restored.confidence == 0.7


@pytest.mark.parametrize("mode", ["auto", "delegated", "human", "ai_assisted", ""])
@pytest.mark.parametrize(
    "model,payload",
    [
        (ReviewModeRequest, {}),
        (AdoptRecommendationsRequest, {"context_hash": "a" * 64, "recommendation_ids": ["id"]}),
        (
            CardConfirmBatchRequest,
            {
                "base_version": 1,
                "context_hash": "a" * 64,
                "field_paths": ["hyperparameters.seed"],
            },
        ),
        (PlanEditRequest, {"base_version": 1, "change_reason": "Choose mode"}),
        (
            CardEditRequest,
            {"base_version": 1, "edits": [{"field_path": "optimizer", "value": "sgd"}]},
        ),
    ],
)
def test_unsupported_review_modes_are_rejected(model, payload, mode):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "review_mode": mode})


@pytest.mark.parametrize("mode", ["auto", "delegated"])
def test_stored_card_and_plan_reject_unsupported_modes(mode):
    for model, instance in [(ReproductionCard, review_card()), (ExperimentPlan, review_plan())]:
        payload = instance.model_dump(mode="json")
        payload["review_mode"] = mode
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_review_request_rejects_extra_evidence_and_empty_scope():
    payload = {"card": review_card().model_dump(mode="json"), "scope": "alice"}
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate({**payload, "evidence_quote": "Invented quote"})
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate({**payload, "scope": ""})


@pytest.mark.parametrize("source", ["delegated", "expert", "assisted"])
def test_unsupported_confirmation_sources_are_rejected(source):
    for model, payload in [
        (EvidenceField, {"value": "sgd", "status": "inferred"}),
        (PlanParam, {"value": "sgd", "source": "inferred", "rationale": "Proposal"}),
        (
            Assumption,
            {
                "field_path": "training_config.optimizer",
                "kind": "inferred",
                "value": "sgd",
                "rationale": "Proposal",
            },
        ),
        (PlanChangeRecord, {"field_path": "optimizer", "before": "sgd", "after": "adam"}),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "confirmed_by": source})


@pytest.mark.parametrize(
    "model,payload",
    [
        (ExplainFieldsRequest, {}),
        (CardExplainRequest, {"base_version": 1}),
        (ReviewModeRequest, {}),
        (AdoptRecommendationsRequest, {"context_hash": "a" * 64, "recommendation_ids": ["id"]}),
        (
            CardConfirmBatchRequest,
            {
                "base_version": 1,
                "context_hash": "a" * 64,
                "field_paths": ["hyperparameters.seed"],
            },
        ),
        (ReviewField, {"field_path": "optimizer", "value": "sgd"}),
        (FieldRecommendation, recommendation()),
        (
            FieldExplanation,
            {
                "field_path": "optimizer",
                "what": "Optimizer",
                "why": "Needed",
                "consequence": "Updates weights",
                "risk": "Wrong convergence",
            },
        ),
        (ExplainResponse, {"context_hash": "a" * 64, "explanations": []}),
        (RecommendResponse, {"context_hash": "a" * 64, "recommendations": []}),
    ],
)
def test_review_contracts_forbid_additional_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "evidence_quote": "Invented quote"})


@pytest.mark.parametrize("value", ["0.1", 1, 0.1, True, False, None])
def test_review_values_preserve_scalar_types(value):
    item = FieldRecommendation.model_validate(recommendation(suggested_value=value))
    assert item.suggested_value == value
    assert type(item.suggested_value) is type(value)


@pytest.mark.parametrize("value", [[], {"value": "sgd"}])
def test_recommendation_does_not_accept_structured_value_overrides(value):
    with pytest.raises(ValidationError):
        FieldRecommendation.model_validate(recommendation(suggested_value=value))


@pytest.mark.parametrize(
    "payload",
    [
        {"context_hash": "short", "recommendation_ids": ["id"]},
        {"context_hash": "a" * 64, "recommendation_ids": []},
        {"context_hash": "a" * 64, "recommendation_ids": ["id", "id"]},
        {"context_hash": "a" * 64, "recommendation_ids": ["id"], "suggested_value": "adam"},
    ],
)
def test_adoption_only_accepts_distinct_ids_and_a_context_hash(payload):
    with pytest.raises(ValidationError):
        AdoptRecommendationsRequest.model_validate(payload)


def test_pending_card_fields_use_extraction_confidence_and_do_not_confirm():
    card = review_card()
    before = card.model_dump(mode="json")
    fields = ReviewRequest(card=card, scope="alice").review_fields()
    by_path = {field.field_path: field for field in fields}
    assert "dataset" not in by_path
    assert "hyperparameters.epochs" not in by_path
    assert by_path["optimizer"].risk_tier == "critical"
    assert by_path["optimizer"].confidence == 0.99
    assert by_path["loss_function"].inferred is False
    assert preselected_paths(fields, 0.9) == [
        "hyperparameters.weight_decay",
        "data_preprocessing.normalize",
    ]
    assert card.model_dump(mode="json") == before


def test_pending_plan_fields_exclude_info_confirmed_and_nonrequired():
    plan = review_plan()
    before = plan.model_dump(mode="json")
    fields = ReviewRequest(card=review_card(), plan=plan, scope="alice").review_fields()
    assert {field.field_path for field in fields} == {
        "training_config.optimizer",
        "training_config.weight_decay",
        "training_config.epochs",
    }
    assert preselected_paths(fields, 0.9) == ["training_config.weight_decay"]
    assert next(f for f in fields if f.field_path.endswith("weight_decay")).confidence == 0.91
    assert plan.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "tier,inferred,acceptable,confidence,selected",
    [
        ("routine", True, True, 0.9, True),
        ("routine", True, True, 0.899, False),
        ("routine", True, False, 1, False),
        ("routine", False, True, 1, False),
        ("critical", True, True, 0.99, False),
        ("critical", True, True, 1, False),
        ("info", True, True, 1, False),
    ],
)
def test_preselection_requires_every_eligibility_condition(
    tier,
    inferred,
    acceptable,
    confidence,
    selected,
):
    field = ReviewField(
        field_path="choice",
        value=1,
        risk_tier=tier,
        inferred=inferred,
        auto_acceptable=acceptable,
        confidence=confidence,
    )
    assert preselected_paths([field], 0.9) == (["choice"] if selected else [])


@pytest.mark.parametrize("with_plan", [False, True])
def test_requested_fields_must_be_exact_pending_paths(with_plan):
    plan = review_plan() if with_plan else None
    pending = "training_config.optimizer" if with_plan else "optimizer"
    nonpending = "training_config.batch_size" if with_plan else "dataset"
    request = ReviewRequest(card=review_card(), plan=plan, scope="alice", field_paths=[pending])
    assert [field.field_path for field in request.review_fields()] == [pending]
    for paths in [[pending, pending], ["unknown"], [nonpending]]:
        request.field_paths = paths
        with pytest.raises(ValueError):
            request.review_fields()


def test_context_hash_is_canonical_and_independent_of_requested_field_list():
    request = ReviewRequest(card=review_card(), plan=review_plan(), scope="alice")
    before = request.model_dump(mode="json")
    digest = request.context_hash()
    assert len(digest) == 64
    int(digest, 16)
    for paths in [["training_config.optimizer"], ["training_config.weight_decay"], []]:
        subset = request.model_copy(deep=True, update={"field_paths": paths})
        assert subset.context_hash() == digest
    reordered = deepcopy(before)
    reordered["card"]["hyperparameters"] = dict(
        reversed(list(reordered["card"]["hyperparameters"].items()))
    )
    assert ReviewRequest.model_validate(reordered).context_hash() == digest
    assert request.model_dump(mode="json") == before


@pytest.mark.parametrize("change", ["card", "confidence", "plan", "scope", "without_plan"])
def test_context_hash_binds_card_value_evidence_plan_and_owner(change):
    original = ReviewRequest(card=review_card(), plan=review_plan(), scope="alice")
    changed = original.model_copy(deep=True)
    if change == "card":
        changed.card.optimizer.value = "adam"
    elif change == "confidence":
        changed.card.optimizer.confidence = 0.5
    elif change == "plan":
        changed.plan.training_config.learning_rate.value = 0.02
    elif change == "scope":
        changed.scope = "bob"
    else:
        changed.plan = None
    assert changed.context_hash() != original.context_hash()


@pytest.mark.parametrize("source", ["human", "ai_assisted"])
@pytest.mark.parametrize(
    "evidence",
    [
        {"status": "missing", "value": "sgd"},
        {"status": "missing", "source_page": 1},
        {"status": "missing", "evidence_quote": "Not actually reported"},
        {"status": "inferred", "value": None},
        {"status": "inferred", "value": "sgd", "source_page": 0},
        {"status": "paper_explicit", "value": "sgd", "source_page": 1},
        {"status": "paper_described", "value": "sgd", "evidence_quote": "SGD"},
        {"status": "paper_explicit", "value": "sgd", "evidence_quote": " ", "source_page": 1},
    ],
)
def test_confirmation_metadata_never_weakens_strict_evidence_rules(source, evidence):
    with pytest.raises(ValidationError):
        EvidenceField.model_validate({**evidence, "human_confirmed": True, "confirmed_by": source})


@pytest.mark.parametrize(
    "param",
    [
        {"value": "sgd", "source": "paper_explicit"},
        {"value": "sgd", "source": "paper_explicit", "evidence_quote": "SGD", "source_page": 0},
        {"value": "sgd", "source": "inferred"},
    ],
)
def test_assisted_confirmation_does_not_bypass_plan_provenance(param):
    with pytest.raises(ValidationError):
        PlanParam.model_validate({**param, "human_confirmed": True, "confirmed_by": "ai_assisted"})


def test_change_record_roundtrips_auditable_advice_without_changing_source_confidence():
    actor = UUID("00000000-0000-0000-0000-000000000001")
    now = datetime.now(UTC)
    record = PlanChangeRecord(
        field_path="training_config.optimizer",
        before="sgd",
        after="sgd",
        actor=actor,
        timestamp=now,
        confirmed_by="ai_assisted",
        review_mode="assisted",
        recommendation_id="server-advice-1",
        recommendation_confidence=0.98,
        recommendation_reasoning="Safe baseline",
        recommendation_risk="May differ",
        preselection_threshold=0.9,
    )
    assert PlanChangeRecord.model_validate_json(record.model_dump_json()) == record
    assert "confidence" not in record.model_dump()
    assert isinstance(record, ReviewDecision)
    assert isinstance(record.id, UUID)
    assert PlanChangeRecord(field_path="mode", before=None, after=None).id != record.id


@pytest.mark.parametrize("consent", [False, None, 1, 0, "true"])
def test_delegation_requires_literal_explicit_consent(consent):
    with pytest.raises(ValidationError):
        DelegateReviewRequest(base_version=1, consent=consent)


def test_delegate_request_requires_version_and_forbids_authorization_injection():
    assert DelegateReviewRequest(base_version=1, consent=True).consent is True
    for payload in [
        {"base_version": 1}, {"consent": True}, {"base_version": 0, "consent": True},
        {"base_version": 1, "consent": True, "delegated_by": str(UUID(int=1))},
    ]:
        with pytest.raises(ValidationError):
            DelegateReviewRequest.model_validate(payload)


@pytest.mark.parametrize("model,payload", [
    (EvidenceField, {"value": "sgd", "status": "inferred"}),
    (PlanParam, {"value": "sgd", "source": "inferred", "rationale": "Proposal"}),
    (Assumption, {"field_path": "optimizer", "kind": "inferred", "value": "sgd",
                  "rationale": "Proposal"}),
])
def test_auto_is_never_a_human_confirmation(model, payload):
    auto = model.model_validate({**payload, "confirmed_by": "auto"})
    assert not auto.human_confirmed
    assert auto.confirmed_by is ConfirmationSource.AUTO
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "confirmed_by": "auto", "human_confirmed": True})
    for source in ("human", "ai_assisted"):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "confirmed_by": source})
        assert model.model_validate({
            **payload, "confirmed_by": source, "human_confirmed": True,
        }).human_confirmed


@pytest.mark.parametrize("status", ["paper_explicit", "paper_described"])
def test_auto_cannot_upgrade_evidence(status):
    with pytest.raises(ValidationError):
        EvidenceField(value="sgd", status=status, evidence_quote="SGD", source_page=1,
                      confirmed_by="auto")


@pytest.mark.parametrize("source", ["paper_explicit", "human_confirmed", "mvp_scaling"])
def test_auto_plan_param_must_remain_inferred(source):
    with pytest.raises(ValidationError):
        PlanParam(value="sgd", source=source, evidence_quote="SGD", source_page=1,
                  rationale="Proposal", confirmed_by="auto")


def test_auto_missing_remains_empty_without_any_evidence():
    missing = EvidenceField(status="missing", confirmed_by="auto")
    assert missing.value is None and not missing.human_confirmed
    for override in [
        {"value": 1}, {"evidence_quote": "invented"}, {"source_page": 1},
        {"source_section": "invented"},
    ]:
        with pytest.raises(ValidationError):
            EvidenceField(status="missing", confirmed_by="auto", **override)


@pytest.mark.parametrize("model,factory", [(ReproductionCard, review_card),
                                          (ExperimentPlan, review_plan)])
def test_delegation_is_paired_aware_and_only_active_in_delegated_mode(model, factory):
    base = factory().model_dump(mode="json")
    valid = {"review_mode": "delegated", "delegated_at": utcnow(), "delegated_by": UUID(int=1)}
    instance = model.model_validate({**base, **valid})
    assert model.model_validate_json(instance.model_dump_json()) == instance
    for override in [
        {"delegated_at": None}, {"delegated_by": None},
        {"delegated_at": datetime(2026, 1, 1)}, {"review_mode": "expert"},
        {"review_mode": "assisted"},
    ]:
        with pytest.raises(ValidationError):
            model.model_validate({**base, **valid, **override})


def test_approval_gate_is_actor_bound_and_revocation_does_not_erase_auto_history():
    data = review_plan().model_dump(mode="json")
    actor = UUID(int=1)
    assert not review_plan().approval_ready()
    for assumption in data["assumptions"]:
        if assumption["requires_confirmation"] and not assumption["human_confirmed"]:
            assumption["confirmed_by"] = "auto"
    data.update(review_mode="delegated", delegated_at=utcnow(), delegated_by=actor)
    plan = ExperimentPlan.model_validate(data)
    assert plan.approval_ready() and plan.approval_ready(actor)
    assert not plan.approval_ready(UUID(int=2))
    assert plan.unconfirmed_critical()  # AUTO was never converted to human.
    paths = {f.field_path for f in ReviewRequest(card=review_card(), plan=plan,
                                                scope="alice").review_fields()}
    assert paths == {"training_config.optimizer", "training_config.weight_decay",
                     "training_config.epochs"}
    data.update(review_mode="assisted", delegated_at=None, delegated_by=None)
    revoked = ExperimentPlan.model_validate(data)
    assert not revoked.approval_ready(actor)
    assert revoked.assumptions[0].confirmed_by is ConfirmationSource.AUTO


def test_delegated_gate_includes_routine_but_ordinary_gate_does_not():
    data = review_plan().model_dump(mode="json")
    data["assumptions"][0].update(human_confirmed=True, confirmed_by="human")
    assert ExperimentPlan.model_validate(data).approval_ready()
    data.update(review_mode="delegated", delegated_at=utcnow(), delegated_by=UUID(int=1))
    assert not ExperimentPlan.model_validate(data).approval_ready()
