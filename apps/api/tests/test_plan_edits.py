from uuid import uuid4

import pytest
from reproagent_api.services import PlanEditError, apply_plan_edits
from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    DatasetConfig,
    EvaluationConfig,
    EvidenceStatus,
    ExpectedRuntime,
    ExperimentPlan,
    ModelConfig,
    ParamSource,
    PlanChange,
    PlanEditRequest,
    PlanOrigin,
    PlanParam,
    PreprocessingConfig,
    ResourceEstimates,
    RuntimeBudget,
    TrainingConfig,
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
