import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    CodeBundle,
    CodeFile,
    CodegenRequest,
    DatasetConfig,
    ErrorCategory,
    ErrorClassification,
    EvaluationConfig,
    EvidenceField,
    EvidenceStatus,
    ExpectedRuntime,
    ExperimentPlan,
    ModelConfig,
    ParamSource,
    PlanParam,
    PreprocessingConfig,
    RepairRequest,
    RepairResponse,
    ReproductionCard,
    ResourceEstimates,
    RuntimeBudget,
    TrainingConfig,
    ValidationIssue,
    ValidationReport,
    compute_code_hash,
)


def make_plan() -> ExperimentPlan:
    return ExperimentPlan(
        rationale="MVP",
        dataset_config=DatasetConfig(train_subset_size=10000, test_subset_size=10000),
        model_cfg=ModelConfig(),
        preprocessing_config=PreprocessingConfig(),
        training_config=TrainingConfig(
            optimizer=PlanParam(value="sgd", source=ParamSource.MVP_SCALING),
            learning_rate=PlanParam(value=0.01, source=ParamSource.MVP_SCALING),
            loss_function=PlanParam(value="cross_entropy", source=ParamSource.MVP_SCALING),
            batch_size=PlanParam(value=64, source=ParamSource.MVP_SCALING),
            epochs=PlanParam(value=3, source=ParamSource.MVP_SCALING),
        ),
        evaluation_config=EvaluationConfig(
            metrics=["accuracy"], target_metric="accuracy", tolerance=0.03, comparison_note="x"
        ),
        runtime_budget=RuntimeBudget(),
        expected_runtime=ExpectedRuntime(estimated_seconds=120, basis="x"),
        resource_estimates=ResourceEstimates(),
    )


def make_card() -> ReproductionCard:
    def ex(v):
        return EvidenceField(
            value=v,
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote=f"...{v}...",
            source_page=1,
        )

    return ReproductionCard(
        method=ex("LeNet-5"),
        model_architecture=ex("LeNet-5"),
        dataset=ex("MNIST"),
        optimizer=ex("sgd"),
        loss_function=ex("cross-entropy"),
    )


def bundle():
    return CodeBundle(
        files=[
            CodeFile(path="train.py", content="print(1)"),
            CodeFile(path="model.py", content="x=1"),
        ],
        requirements=["torch==2.4.1"],
    )


class TestCodeBundle:
    def test_code_hash_autocomputed(self):
        b = bundle()
        assert len(b.code_hash) == 64

    def test_hash_order_independent(self):
        f1 = CodeFile(path="a.py", content="1")
        f2 = CodeFile(path="b.py", content="2")
        assert compute_code_hash([f1, f2]) == compute_code_hash([f2, f1])

    def test_hash_changes_with_content(self):
        b1 = bundle()
        b2 = CodeBundle(
            files=[
                CodeFile(path="train.py", content="print(2)"),
                CodeFile(path="model.py", content="x=1"),
            ],
            requirements=["torch==2.4.1"],
        )
        assert b1.code_hash != b2.code_hash

    def test_non_whitelisted_requirement_rejected(self):
        with pytest.raises(ValidationError):
            CodeBundle(
                files=[CodeFile(path="train.py", content="x")], requirements=["requests==2.0"]
            )

    def test_missing_entrypoint_rejected(self):
        with pytest.raises(ValidationError):
            CodeBundle(files=[CodeFile(path="model.py", content="x")])


class TestCodegenContracts:
    def test_codegen_request_requires_card_and_plan(self):
        req = CodegenRequest(card=make_card(), plan=make_plan())
        assert req.plan.training_config.epochs.value == 3
        with pytest.raises(ValidationError):
            CodegenRequest(card=make_card())

    def test_validation_report(self):
        report = ValidationReport(
            ok=False, issues=[ValidationIssue(code="BANNED_IMPORT", message="os")]
        )
        assert not report.ok
        assert report.issues[0].code == "BANNED_IMPORT"

    def test_repair_response_replan_flag(self):
        resp = RepairResponse(
            bundle=bundle(), change_reason="OOM reduce batch", replan_required=True
        )
        assert resp.replan_required is True

    def test_repair_request_classification(self):
        req = RepairRequest(
            bundle=bundle(),
            classification=ErrorClassification(
                category=ErrorCategory.SHAPE_MISMATCH, reason="size"
            ),
            plan=make_plan(),
        )
        assert req.classification.category is ErrorCategory.SHAPE_MISMATCH


def test_error_category_has_eight_values():
    assert len(ErrorCategory) == 8
    assert ErrorCategory.DATA_DOWNLOAD_ERROR.value == "DATA_DOWNLOAD_ERROR"
