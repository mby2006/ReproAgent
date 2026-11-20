from datetime import datetime
from uuid import uuid4

import pytest
from reproagent_api.services import PlanEditError, apply_delegated_review, apply_plan_edits
from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    ConfirmationSource,
    DatasetConfig,
    EvaluationConfig,
    EvidenceStatus,
    ExpectedRuntime,
    ExperimentPlan,
    FieldRecommendation,
    ModelConfig,
    ParamSource,
    PlanChange,
    PlanEditRequest,
    PlanOrigin,
    PlanParam,
    PreprocessingConfig,
    ResourceEstimates,
    ReviewMode,
    RiskTier,
    RuntimeBudget,
    TrainingConfig,
    utcnow,
)


def make_plan() -> ExperimentPlan:
    return ExperimentPlan(
        rationale="MVP plan",
        dataset_config=DatasetConfig(train_subset_size=10000, test_subset_size=10000),
        model_cfg=ModelConfig(),
        preprocessing_config=PreprocessingConfig(),
        training_config=TrainingConfig(
            optimizer=PlanParam(value="sgd", source=ParamSource.INFERRED, rationale="proposal"),
            learning_rate=PlanParam(
                value=0.001,
                source=ParamSource.PAPER_EXPLICIT,
                evidence_quote="lr 0.001",
                source_page=8,
            ),
            loss_function=PlanParam(
                value="cross_entropy",
                source=ParamSource.INFERRED,
                rationale="论文未明确",
                requires_confirmation=True,
            ),
            batch_size=PlanParam(value=64, source=ParamSource.MVP_SCALING, paper_value=128),
            epochs=PlanParam(value=3, source=ParamSource.MVP_SCALING, paper_value=20),
            seed=42,
        ),
        evaluation_config=EvaluationConfig(
            metrics=["accuracy"], target_metric="accuracy", tolerance=0.03, comparison_note="scaled"
        ),
        runtime_budget=RuntimeBudget(),
        expected_runtime=ExpectedRuntime(estimated_seconds=120, basis="estimate"),
        resource_estimates=ResourceEstimates(),
        assumptions=[
            Assumption(
                field_path="training_config.loss_function",
                kind=AssumptionKind.MISSING_PROPOSAL,
                value="cross_entropy",
                rationale="论文未明确损失函数",
                requires_confirmation=True,
                card_status=EvidenceStatus.MISSING,
            )
        ],
    )


def edit(plan, path, value, confirmed=True, reason="调整"):
    request = PlanEditRequest(
        base_version=1,
        changes=[PlanChange(field_path=path, value=value, human_confirmed=confirmed)],
        change_reason=reason,
    )
    return apply_plan_edits(plan, request)


def test_edit_param_records_before_after():
    plan = make_plan()
    new_plan, records = edit(plan, "training_config.batch_size", 32)
    assert new_plan.training_config.batch_size.value == 32
    assert records[0].before == 64
    assert records[0].after == 32
    # base plan untouched
    assert plan.training_config.batch_size.value == 64


def test_confirm_clears_assumption_and_gate():
    plan = make_plan()
    assert len(plan.unconfirmed_required()) == 1
    new_plan, _ = edit(plan, "training_config.loss_function", "cross_entropy")
    assert new_plan.training_config.loss_function.human_confirmed is True
    assert new_plan.training_config.loss_function.source is ParamSource.HUMAN_CONFIRMED
    assert new_plan.training_config.loss_function.requires_confirmation is False
    assert new_plan.unconfirmed_required() == []
    assert new_plan.assumptions[0].human_confirmed is True


def test_edit_scalar_budget():
    plan = make_plan()
    new_plan, records = edit(plan, "runtime_budget.max_seconds", 300)
    assert new_plan.runtime_budget.max_seconds == 300
    assert records[0].before == 600


def test_edit_unknown_field_rejected():
    plan = make_plan()
    with pytest.raises(PlanEditError):
        edit(plan, "model_config.architecture", "resnet")


def test_edit_invalid_value_rejected():
    plan = make_plan()
    with pytest.raises(PlanEditError):
        edit(plan, "training_config.learning_rate", "not-a-number")


def test_oom_retry_origin_and_reason_preserved():
    plan = make_plan()
    request = PlanEditRequest(
        base_version=1,
        changes=[PlanChange(field_path="training_config.batch_size", value=16)],
        change_reason="OOM：降低批大小",
        origin=PlanOrigin.OOM_RETRY,
        related_error="CUDA out of memory",
        related_run_id=uuid4(),
    )
    new_plan, records = apply_plan_edits(plan, request)
    assert new_plan.training_config.batch_size.value == 16
    assert request.origin is PlanOrigin.OOM_RETRY
    assert records[0].after == 16


def test_edit_missing_weight_decay_creates_param():
    plan = make_plan()
    assert plan.training_config.weight_decay is None
    new_plan, _ = edit(plan, "training_config.weight_decay", 0.0005)
    assert new_plan.training_config.weight_decay is not None
    assert new_plan.training_config.weight_decay.value == 0.0005


def advice_for(plan):
    return [FieldRecommendation(
        id=f"advice-{index}", field_path=a.field_path, action="accept", suggested_value=a.value,
        confidence=0.97, reasoning="Conservative proposal", risk="Not original paper evidence",
    ) for index, a in enumerate(plan.unconfirmed_required()) if a.risk_tier is not RiskTier.INFO]


def pending_plan():
    plan = make_plan()
    plan.training_config.loss_function.paper_value = "original loss"
    plan.training_config.loss_function.evidence_quote = "Context about training"
    plan.training_config.loss_function.source_page = 4
    plan.training_config.weight_decay = PlanParam(
        value=0.01, source=ParamSource.INFERRED, rationale="Proposal", requires_confirmation=True,
    )
    plan.assumptions.append(Assumption(
        field_path="training_config.weight_decay", kind=AssumptionKind.INFERRED,
        value=0.01, rationale="Proposal", risk_tier=RiskTier.ROUTINE,
    ))
    return plan


def test_delegated_review_audits_auto_and_preserves_evidence_and_original():
    plan = pending_plan()
    before = plan.model_dump(mode="json")
    actor, at = uuid4(), utcnow()
    advice = advice_for(plan)
    advice[1] = advice[1].model_copy(update={"action": "modify", "suggested_value": 0.02})
    new, records = apply_delegated_review(plan, advice, actor=actor, delegated_at=at)
    assert plan.model_dump(mode="json") == before
    assert new.review_mode is ReviewMode.DELEGATED
    assert new.delegated_by == actor and new.delegated_at == at
    assert new.approval_ready(actor) and not new.approval_ready(uuid4())
    assert new.unconfirmed_critical()
    assert new.training_config.weight_decay.value == 0.02
    for param in (new.training_config.loss_function, new.training_config.weight_decay):
        assert not param.human_confirmed and param.requires_confirmation
        assert param.confirmed_by is ConfirmationSource.AUTO
        assert param.source is ParamSource.INFERRED
    for assumption in new.assumptions:
        assert not assumption.human_confirmed and assumption.requires_confirmation
        assert assumption.confirmed_by is ConfirmationSource.AUTO
    for key in ("paper_value", "evidence_quote", "source_page"):
        assert getattr(new.training_config.loss_function, key) == getattr(
            plan.training_config.loss_function, key,
        )
    assert records[0].field_path == "review_mode"
    assert records[0].before is ReviewMode.ASSISTED
    assert records[0].after is ReviewMode.DELEGATED
    assert records[0].confirmed_by is None
    for record, recommendation in zip(records[1:], advice, strict=True):
        assert record.confirmed_by is ConfirmationSource.AUTO
        assert record.recommendation_id == recommendation.id
        assert record.recommendation_confidence == recommendation.confidence
        assert record.recommendation_reasoning == recommendation.reasoning
        assert record.recommendation_risk == recommendation.risk
    assert all(r.actor == actor and r.timestamp >= at for r in records)
    assert len({r.timestamp for r in records}) == 1
    assert len({r.id for r in records}) == len(records)


@pytest.mark.parametrize("invalid", [
    "missing", "duplicate_path", "duplicate_id", "nonpending", "info", "accept_changed",
    "bool_accept", "bool_modify", "string_numeric", "nonfinite", "stale_assumption",
    "absent_executable",
])
def test_delegated_review_requires_complete_precise_pending_advice(invalid):
    plan = pending_plan()
    advice = advice_for(plan)
    if invalid == "missing":
        advice.pop()
    elif invalid == "duplicate_path":
        advice.append(advice[0])
    elif invalid == "duplicate_id":
        advice[1] = advice[1].model_copy(update={"id": advice[0].id})
    elif invalid in ("nonpending", "info"):
        if invalid == "info":
            plan.assumptions[1].risk_tier = RiskTier.INFO
        else:
            plan.assumptions[1].requires_confirmation = False
    elif invalid == "accept_changed":
        advice[0] = advice[0].model_copy(update={"suggested_value": "mse"})
    elif invalid in ("bool_accept", "bool_modify"):
        plan.training_config.weight_decay.value = 1.0
        plan.assumptions[1].value = 1.0
        advice[1] = advice[1].model_copy(update={
            "action": "accept" if invalid == "bool_accept" else "modify", "suggested_value": True,
        })
    elif invalid == "string_numeric":
        advice[1] = advice[1].model_copy(update={"action": "modify", "suggested_value": "0.02"})
    elif invalid == "nonfinite":
        advice[1] = advice[1].model_copy(update={
            "action": "modify", "suggested_value": float("inf"),
        })
    elif invalid == "absent_executable":
        plan.training_config.weight_decay = None
    else:
        plan.training_config.weight_decay.value = 0.03
    before = plan.model_dump(mode="json")
    with pytest.raises(PlanEditError):
        apply_delegated_review(plan, advice, actor=uuid4())
    assert plan.model_dump(mode="json") == before


def test_unknown_assumption_can_only_be_accepted_unchanged():
    plan = make_plan()
    plan.assumptions.append(Assumption(
        field_path="model_config.architecture", kind="inferred", value="lenet-5",
        rationale="Architecture inference",
    ))
    original = plan.model_dump(mode="json")
    advice = advice_for(plan)
    new, records = apply_delegated_review(plan, advice, actor=uuid4())
    assert new.model_cfg == plan.model_cfg
    assert new.assumptions[-1].confirmed_by is ConfirmationSource.AUTO
    assert records[-1].before == records[-1].after == "lenet-5"
    for change in [{"action": "modify"}, {"suggested_value": "resnet"}]:
        bad = [*advice[:-1], advice[-1].model_copy(update=change)]
        with pytest.raises(PlanEditError):
            apply_delegated_review(plan, bad, actor=uuid4())
    assert plan.model_dump(mode="json") == original


@pytest.mark.parametrize("mode,origin", [
    (None, PlanOrigin.USER_EDIT), (ReviewMode.EXPERT, PlanOrigin.USER_EDIT),
    (None, PlanOrigin.OOM_RETRY),
])
def test_any_ordinary_edit_revokes_delegation_but_retains_unrelated_auto(mode, origin):
    plan, _ = apply_delegated_review(pending_plan(), advice_for(pending_plan()), actor=uuid4())
    before = plan.model_dump(mode="json")
    new, records = apply_plan_edits(plan, PlanEditRequest(
        base_version=1, changes=[PlanChange(field_path="training_config.batch_size", value=16)],
        change_reason="Edit or OOM retry", origin=origin, review_mode=mode,
    ))
    assert new.delegated_at is None and new.delegated_by is None
    assert new.review_mode is (mode or ReviewMode.ASSISTED)
    assert new.assumptions[0].confirmed_by is ConfirmationSource.AUTO
    assert not new.approval_ready() and new.unconfirmed_critical()
    assert records[0].field_path == "review_mode"
    assert records[0].before is ReviewMode.DELEGATED
    assert plan.model_dump(mode="json") == before


def test_mode_only_revokes_and_ai_adoption_does_not_retain_authorization():
    actor = uuid4()
    plan = pending_plan()
    delegated, _ = apply_delegated_review(plan, advice_for(plan), actor=actor)
    ordinary, records = apply_plan_edits(delegated, PlanEditRequest(
        base_version=1, review_mode="expert", change_reason="Revoke",
    ), actor=actor)
    assert not ordinary.approval_ready()
    assert records[0].actor == actor
    advice = advice_for(delegated)[0]
    edited, _ = apply_plan_edits(delegated, PlanEditRequest(
        base_version=1, change_reason="Adopt manually", changes=[PlanChange(
            field_path=advice.field_path, value=advice.suggested_value,
        )],
    ), actor=actor, confirmed_by=ConfirmationSource.AI_ASSISTED,
        recommendations={advice.field_path: advice})
    assert edited.delegated_by is None and edited.review_mode is ReviewMode.ASSISTED
    assert edited.training_config.loss_function.human_confirmed
    assert edited.training_config.loss_function.confirmed_by is ConfirmationSource.AI_ASSISTED
    assert edited.training_config.loss_function.source is ParamSource.INFERRED
    assert edited.assumptions[1].confirmed_by is ConfirmationSource.AUTO


def test_delegated_authorization_rejects_another_actor_or_naive_time():
    actor = uuid4()
    plan = pending_plan()
    delegated, _ = apply_delegated_review(plan, advice_for(plan), actor=actor)
    with pytest.raises(PlanEditError, match="another actor"):
        apply_delegated_review(delegated, advice_for(delegated), actor=uuid4())
    with pytest.raises(PlanEditError, match="timezone aware"):
        apply_delegated_review(
            plan, advice_for(plan), actor=actor, delegated_at=datetime(2026, 1, 1),
        )
    with pytest.raises(PlanEditError, match="actor"):
        apply_delegated_review(plan, advice_for(plan), actor=None)


@pytest.mark.parametrize("value", [601, 0, -1, True, "300", 300.5])
def test_delegation_rejects_runtime_outside_authorized_bounds(value):
    plan = make_plan()
    plan.assumptions.append(Assumption(
        field_path="runtime_budget.max_seconds", kind="inferred", value=600,
        rationale="Authorized runtime ceiling",
    ))
    advice = advice_for(plan)
    advice[-1] = advice[-1].model_copy(update={"action": "modify", "suggested_value": value})
    with pytest.raises(PlanEditError):
        apply_delegated_review(plan, advice, actor=uuid4())
    assert plan.runtime_budget.max_seconds == 600


@pytest.mark.parametrize("path,value", [
    ("training_config.learning_rate", 0), ("training_config.learning_rate", -0.1),
    ("training_config.weight_decay", -0.1),
])
def test_delegation_rejects_unsafe_training_values(path, value):
    plan = pending_plan()
    if path.endswith("learning_rate"):
        plan.assumptions.append(Assumption(
            field_path=path, kind="inferred", value=0.001, rationale="Proposal",
        ))
    advice = [
        r.model_copy(update={"action": "modify", "suggested_value": value})
        if r.field_path == path else r for r in advice_for(plan)
    ]
    with pytest.raises(PlanEditError):
        apply_delegated_review(plan, advice, actor=uuid4())


def test_delegation_can_reduce_runtime_within_authorized_budget():
    plan = make_plan()
    plan.assumptions.append(Assumption(
        field_path="runtime_budget.max_seconds", kind="inferred", value=600,
        rationale="Authorized runtime ceiling",
    ))
    advice = advice_for(plan)
    advice[-1] = advice[-1].model_copy(update={"action": "modify", "suggested_value": 300})
    new, records = apply_delegated_review(plan, advice, actor=uuid4())
    assert new.runtime_budget.max_seconds == 300
    assert records[-1].before == 600 and records[-1].after == 300
    assert records[-1].confirmed_by is ConfirmationSource.AUTO


def test_ordinary_service_cannot_be_used_for_auto_acceptance():
    with pytest.raises(PlanEditError, match="delegated review"):
        apply_plan_edits(make_plan(), PlanEditRequest(
            base_version=1, review_mode="assisted", change_reason="Cannot grant authority",
        ), confirmed_by=ConfirmationSource.AUTO)
