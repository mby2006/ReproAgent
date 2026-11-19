from uuid import uuid4

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    CheckStatus,
    DatasetSpec,
    EvaluationSpec,
    Evidence,
    FinalStatus,
    HealthResponse,
    JobAccepted,
    JobStatus,
    MetricComparison,
    ModelSpec,
    Paper,
    PaperSource,
    ReproductionJob,
    ReproductionSpec,
    TrainingSpec,
)

USER_ID = uuid4()
JOB_ID = uuid4()
PAPER_ID = uuid4()
PROJECT_ID = uuid4()


def make_spec() -> ReproductionSpec:
    return ReproductionSpec(
        method_summary="LeNet-5 trained on MNIST with SGD.",
        model=ModelSpec(architecture="lenet-5", input_shape=[1, 28, 28], num_classes=10),
        dataset=DatasetSpec(name="mnist", num_classes=10, train_size=60000, test_size=10000),
        training=TrainingSpec(
            optimizer="sgd", learning_rate=0.01, batch_size=64, epochs=10, loss="cross_entropy"
        ),
        evaluation=EvaluationSpec(metrics=["accuracy"], reported_results={"accuracy": 0.99}),
        evidence=[Evidence(quote="We use LeNet-5 on MNIST.", page=3, section="Experiments")],
    )


class TestOwnership:
    def test_owned_model_requires_user_id(self):
        with pytest.raises(ValidationError):
            ReproductionJob(paper_id=PAPER_ID)

    def test_paper_requires_project_id(self):
        with pytest.raises(ValidationError):
            Paper(title="LeNet", source=PaperSource.URL, source_url="https://example.org/p.pdf")

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            ReproductionJob(user_id=USER_ID, paper_id=PAPER_ID, admin=True)


class TestPaper:
    def test_url_paper_requires_source_url(self):
        with pytest.raises(ValidationError):
            Paper(project_id=PROJECT_ID, title="LeNet", source=PaperSource.URL)

    def test_upload_paper_requires_pdf_path(self):
        with pytest.raises(ValidationError):
            Paper(project_id=PROJECT_ID, title="LeNet", source=PaperSource.UPLOAD)

    def test_valid_upload_paper(self):
        paper = Paper(
            project_id=PROJECT_ID,
            title="LeNet-5",
            source=PaperSource.UPLOAD,
            pdf_path=f"{USER_ID}/paper.pdf",
        )
        assert paper.id is not None
        assert paper.created_at.tzinfo is not None


class TestSpec:
    def test_valid_lenet_spec(self):
        spec = make_spec()
        assert spec.model.architecture == "lenet-5"
        assert spec.evidence[0].page == 3

    def test_spec_requires_evidence(self):
        with pytest.raises(ValidationError):
            ReproductionSpec(**{**make_spec().model_dump(), "evidence": []})

    def test_empty_quote_rejected(self):
        with pytest.raises(ValidationError):
            Evidence(quote="   ")

    def test_learning_rate_must_be_positive(self):
        with pytest.raises(ValidationError):
            TrainingSpec(
                optimizer="sgd", learning_rate=0, batch_size=64, epochs=1, loss="cross_entropy"
            )

    def test_evaluation_requires_metrics(self):
        with pytest.raises(ValidationError):
            EvaluationSpec(metrics=[])


class TestJobsAndHealth:
    def test_job_accepted_defaults_to_queued(self):
        accepted = JobAccepted(job_id=JOB_ID)
        assert accepted.status is JobStatus.QUEUED

    def test_health_response_defaults(self):
        health = HealthResponse(service="api")
        assert health.status.value == "ok"
        assert health.details == {}


class TestReport:
    def test_metric_comparison_with_both_values(self):
        comparison = MetricComparison(
            metric="accuracy",
            paper_value=0.99,
            experiment_value=0.97,
            absolute_difference=0.02,
            relative_difference=0.02 / 0.99,
            percentage_point_difference=-2.0,
            unit="fraction",
            comparison_status=CheckStatus.MATCHED,
            tolerance=0.03,
        )
        assert comparison.percentage_point_difference == pytest.approx(-2.0)

    def test_metric_comparison_missing_value_forbids_differences(self):
        with pytest.raises(ValidationError):
            MetricComparison(
                metric="accuracy",
                experiment_value=0.97,
                absolute_difference=0.02,
                unit="fraction",
                comparison_status=CheckStatus.UNKNOWN,
            )

    def test_metric_comparison_matched_requires_both_values(self):
        with pytest.raises(ValidationError):
            MetricComparison(
                metric="accuracy",
                experiment_value=0.97,
                unit="fraction",
                comparison_status=CheckStatus.MATCHED,
            )

    def test_metric_comparison_without_paper_value_is_unknown(self):
        comparison = MetricComparison(
            metric="test_loss",
            experiment_value=0.3,
            unit="nats",
            comparison_status=CheckStatus.NOT_APPLICABLE,
        )
        assert comparison.absolute_difference is None

    def test_final_status_has_no_full_reproduction(self):
        values = {s.value for s in FinalStatus}
        assert values == {
            "METHOD_VALIDATED",
            "SCALED_RUN_COMPLETED",
            "PARTIAL_MATCH",
            "DEVIATION_FOUND",
            "INSUFFICIENT_EVIDENCE",
            "EXECUTION_FAILED",
        }
