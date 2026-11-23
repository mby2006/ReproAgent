"""Invariants of the paired-comparison contracts."""

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    ComparisonGroup,
    ComparisonPlan,
    ComparisonQuestion,
    ComparisonStatus,
    DatasetConfig,
    EvaluationConfig,
    ExpectedRuntime,
    ExperimentPlan,
    GroupKey,
    ModelConfig,
    PairVerification,
    ParamSource,
    PendingParam,
    PendingStatus,
    PlanParam,
    PreprocessingConfig,
    ResourceEstimates,
    RuntimeBudget,
    ScalingAuthorization,
    SupportCheck,
    SupportGap,
    TrainingConfig,
)


def base_plan() -> ExperimentPlan:
    return ExperimentPlan(
        rationale="scaled",
        dataset_config=DatasetConfig(train_subset_size=4096, test_subset_size=1024),
        model_cfg=ModelConfig(),
        preprocessing_config=PreprocessingConfig(),
        training_config=TrainingConfig(
            optimizer=PlanParam(value="sgd", source=ParamSource.MVP_SCALING),
            learning_rate=PlanParam(value=0.01, source=ParamSource.MVP_SCALING),
            loss_function=PlanParam(
                value="cross_entropy", source=ParamSource.INFERRED, rationale="not stated"
            ),
            batch_size=PlanParam(value=64, source=ParamSource.MVP_SCALING),
            epochs=PlanParam(value=2, source=ParamSource.MVP_SCALING),
        ),
        evaluation_config=EvaluationConfig(
            metrics=["accuracy"], target_metric="accuracy", tolerance=0.03, comparison_note="scaled"
        ),
        runtime_budget=RuntimeBudget(),
        expected_runtime=ExpectedRuntime(estimated_seconds=60, basis="estimate"),
        resource_estimates=ResourceEstimates(),
    )


def group(key: GroupKey, name: str) -> ComparisonGroup:
    evidence = dict(source=ParamSource.PAPER_EXPLICIT, evidence_quote="q", source_page=3)
    return ComparisonGroup(
        key=key,
        label=name,
        optimizer=PlanParam(value=name, **evidence),
        learning_rate=PlanParam(value=0.01, **evidence),
    )


def authorization() -> ScalingAuthorization:
    return ScalingAuthorization(
        max_train_samples=60000,
        max_test_samples=10000,
        max_epochs=20,
        max_seconds=600,
        note="n",
    )


def question() -> ComparisonQuestion:
    return ComparisonQuestion(
        question="q?", restatement="r", target_metric="accuracy"
    )


def test_groups_must_use_different_optimizers():
    with pytest.raises(ValidationError, match="different optimizers"):
        ComparisonPlan(
            question=question(),
            base_plan=base_plan(),
            groups=[group(GroupKey.A, "sgd"), group(GroupKey.B, "sgd")],
            seeds=[42],
            authorization=authorization(),
            support=SupportCheck(ok=True, supported=["x"]),
        )


def test_plan_requires_one_group_per_key():
    with pytest.raises(ValidationError, match="group a and one group b"):
        ComparisonPlan(
            question=question(),
            base_plan=base_plan(),
            groups=[group(GroupKey.A, "sgd"), group(GroupKey.A, "adam")],
            seeds=[42],
            authorization=authorization(),
            support=SupportCheck(ok=True, supported=["x"]),
        )


def test_open_pending_blocks_approval():
    plan = ComparisonPlan(
        question=question(),
        base_plan=base_plan(),
        groups=[group(GroupKey.A, "sgd"), group(GroupKey.B, "adam")],
        seeds=[42],
        authorization=authorization(),
        support=SupportCheck(ok=True, supported=["x"]),
        pending=[
            PendingParam(
                id="p1",
                field_path="groups.a.learning_rate",
                label="lr",
                reason="r",
                impact="i",
            )
        ],
    )
    assert not plan.approval_ready()
    assert len(plan.open_pending) == 1


def test_support_check_must_be_consistent():
    with pytest.raises(ValidationError):
        SupportCheck(ok=True, missing=[SupportGap(field="f", detail="d", expectation="e")])
    with pytest.raises(ValidationError):
        SupportCheck(ok=False)


def test_pending_item_requires_evidence_when_resolved():
    with pytest.raises(ValidationError, match="resolved_value"):
        PendingParam(
            id="p1",
            field_path="groups.a.learning_rate",
            label="lr",
            reason="r",
            impact="i",
            status=PendingStatus.ADOPTED,
        )
    resolved = PendingParam(
        id="p1",
        field_path="groups.a.learning_rate",
        label="lr",
        reason="r",
        impact="i",
        status=PendingStatus.PROVIDED,
        resolved_value=0.01,
        resolved_source="方法章节 p.4",
    )
    assert resolved.resolved


def test_verified_pair_cannot_carry_blockers():
    with pytest.raises(ValidationError):
        PairVerification(seed=42, verified=True, blocking=["mismatch"])


def test_status_enum_keeps_unfinished_states():
    assert ComparisonStatus.COMPLETED == "completed"
    assert {s.value for s in ComparisonStatus} >= {"incomplete", "failed"}
