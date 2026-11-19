from uuid import uuid4

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    DatasetConfig,
    Deviation,
    EvaluationConfig,
    ExpectedRuntime,
    ExperimentPlan,
    ModelConfig,
    ParamSource,
    PlanChange,
    PlanChangeRecord,
    PlanEditRequest,
    PlanOrigin,
    PlanParam,
    PlanResponse,
    PlanVersion,
    PreprocessingConfig,
    ResourceEstimates,
    RiskTier,
    RuntimeBudget,
    TrainingConfig,
)


def make_plan(**overrides) -> ExperimentPlan:
    base = dict(
        rationale="MVP scaled LeNet-5/MNIST plan",
        dataset_config=DatasetConfig(train_subset_size=10000, test_subset_size=10000),
        model_cfg=ModelConfig(),
        preprocessing_config=PreprocessingConfig(),
        training_config=TrainingConfig(
            optimizer=PlanParam(value="sgd", source=ParamSource.MVP_SCALING),
            learning_rate=PlanParam(
                value=0.001,
                source=ParamSource.PAPER_EXPLICIT,
                evidence_quote="lr 0.001",
                source_page=8,
            ),
            loss_function=PlanParam(
                value="cross_entropy", source=ParamSource.INFERRED, rationale="论文未明确"
            ),
            batch_size=PlanParam(value=64, source=ParamSource.MVP_SCALING),
            epochs=PlanParam(value=3, source=ParamSource.MVP_SCALING),
        ),
        evaluation_config=EvaluationConfig(
            metrics=["accuracy"], target_metric="accuracy", tolerance=0.03, comparison_note="scaled"
        ),
        runtime_budget=RuntimeBudget(),
        expected_runtime=ExpectedRuntime(estimated_seconds=120, basis="estimate"),
        resource_estimates=ResourceEstimates(),
    )
    base.update(overrides)
    return ExperimentPlan(**base)


class TestPlanParam:
    def test_paper_explicit_requires_evidence(self):
        with pytest.raises(ValidationError):
            PlanParam(value=0.01, source=ParamSource.PAPER_EXPLICIT)
        with pytest.raises(ValidationError):
            PlanParam(value=0.01, source=ParamSource.PAPER_EXPLICIT, evidence_quote="q")

    def test_inferred_requires_rationale(self):
        with pytest.raises(ValidationError):
            PlanParam(value="sgd", source=ParamSource.INFERRED)

    def test_blank_quote_rejected(self):
        with pytest.raises(ValidationError):
            PlanParam(value="sgd", source=ParamSource.MVP_SCALING, evidence_quote="   ")

    def test_mvp_scaling_needs_no_evidence(self):
        param = PlanParam(value=64, source=ParamSource.MVP_SCALING, paper_value=128)
        assert param.paper_value == 128


class TestPlanSections:
    def test_dataset_subset_positive(self):
        with pytest.raises(ValidationError):
            DatasetConfig(train_subset_size=0, test_subset_size=100)

    def test_evaluation_requires_metrics(self):
        with pytest.raises(ValidationError):
            EvaluationConfig(
                metrics=[], target_metric="accuracy", tolerance=0.0, comparison_note="x"
            )

    def test_evaluation_tolerance_nonnegative(self):
        with pytest.raises(ValidationError):
            EvaluationConfig(
                metrics=["accuracy"], target_metric="accuracy", tolerance=-0.1, comparison_note="x"
            )

    def test_runtime_budget_positive(self):
        with pytest.raises(ValidationError):
            RuntimeBudget(max_seconds=0)


class TestExperimentPlan:
    def test_model_config_alias_roundtrip(self):
        plan = make_plan()
        dumped = plan.model_dump(mode="json")
        assert "model_config" in dumped
        assert "model_cfg" not in dumped
        restored = ExperimentPlan.model_validate(dumped)
        assert restored.model_cfg.architecture == "lenet-5"

    def test_nested_serialization_keeps_alias(self):
        version = PlanVersion(
            experiment_id=uuid4(), version=1, origin=PlanOrigin.GENERATED, plan=make_plan()
        )
        assert "model_config" in version.model_dump(mode="json")["plan"]
        response = PlanResponse(plan=make_plan())
        assert "model_config" in response.model_dump(mode="json")["plan"]

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            make_plan(unknown_field=1)

    def test_unconfirmed_required_filters(self):
        plan = make_plan(
            assumptions=[
                Assumption(
                    field_path="training_config.loss_function",
                    kind=AssumptionKind.MISSING_PROPOSAL,
                    value="cross_entropy",
                    rationale="missing",
                    requires_confirmation=True,
                ),
                Assumption(
                    field_path="dataset_config",
                    kind=AssumptionKind.MVP_SCALING,
                    value="mnist",
                    rationale="scaling",
                    requires_confirmation=False,
                ),
                Assumption(
                    field_path="training_config.optimizer",
                    kind=AssumptionKind.NONSTANDARD_CHOICE,
                    value="sgd",
                    rationale="nonstandard",
                    requires_confirmation=True,
                    human_confirmed=True,
                ),
            ]
        )
        unconfirmed = plan.unconfirmed_required()
        assert len(unconfirmed) == 1
        assert unconfirmed[0].field_path == "training_config.loss_function"


class TestRiskTier:
    def test_default_tier_is_critical(self):
        a = Assumption(
            field_path="training_config.loss_function",
            kind=AssumptionKind.MISSING_PROPOSAL,
            value="cross_entropy",
            rationale="missing",
        )
        assert a.risk_tier is RiskTier.CRITICAL
        assert a.auto_acceptable is False

    def test_unconfirmed_critical_ignores_routine_and_info(self):
        plan = make_plan(
            assumptions=[
                Assumption(
                    field_path="training_config.loss_function",
                    kind=AssumptionKind.MISSING_PROPOSAL,
                    value="cross_entropy",
                    rationale="missing",
                    risk_tier=RiskTier.CRITICAL,
                ),
                Assumption(
                    field_path="training_config.weight_decay",
                    kind=AssumptionKind.MISSING_PROPOSAL,
                    value=0.0,
                    rationale="wd",
                    risk_tier=RiskTier.ROUTINE,
                    auto_acceptable=True,
                ),
                Assumption(
                    field_path="dataset_config",
                    kind=AssumptionKind.MVP_SCALING,
                    value="mnist",
                    rationale="scale",
                    requires_confirmation=False,
                    risk_tier=RiskTier.INFO,
                ),
            ]
        )
        assert len(plan.unconfirmed_required()) == 2
        crit = plan.unconfirmed_critical()
        assert len(crit) == 1
        assert crit[0].field_path == "training_config.loss_function"


class TestPlanContracts:
    def test_change_field_path_pattern(self):
        PlanChange(field_path="training_config.batch_size", value=32)
        with pytest.raises(ValidationError):
            PlanChange(field_path="TrainingConfig.batch_size", value=32)
        with pytest.raises(ValidationError):
            PlanChange(field_path="training_config..batch_size", value=32)

    def test_edit_request_requires_changes_and_reason(self):
        with pytest.raises(ValidationError):
            PlanEditRequest(base_version=1, changes=[], change_reason="x")
        with pytest.raises(ValidationError):
            PlanEditRequest(
                base_version=1,
                changes=[PlanChange(field_path="runtime_budget.max_seconds", value=300)],
                change_reason="",
            )

    def test_edit_request_base_version_positive(self):
        with pytest.raises(ValidationError):
            PlanEditRequest(
                base_version=0,
                changes=[PlanChange(field_path="runtime_budget.max_seconds", value=300)],
                change_reason="x",
            )

    def test_change_record_and_deviation(self):
        record = PlanChangeRecord(field_path="training_config.batch_size", before=64, after=32)
        assert record.after == 32
        deviation = Deviation(
            field_path="training_config.epochs", paper_value="20", plan_value="3", reason="MVP"
        )
        assert deviation.plan_value == "3"
