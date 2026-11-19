from uuid import uuid4

import pytest
from pydantic import ValidationError
from reproagent_schemas import (
    CodeFile,
    ErrorCategory,
    MetricsReport,
    ResourceBudget,
    RunAudit,
    RunRequest,
    RunResult,
    RunStatus,
    is_whitelisted,
)

EXP_ID = uuid4()
PLAN_ID = uuid4()


def files():
    return [CodeFile(path="train.py", content="print('hi')")]


class TestResourceBudget:
    def test_defaults(self):
        budget = ResourceBudget()
        assert budget.max_seconds == 600
        assert budget.device == "cpu"

    def test_hard_timeout_cap(self):
        with pytest.raises(ValidationError):
            ResourceBudget(max_seconds=601)
        with pytest.raises(ValidationError):
            ResourceBudget(max_seconds=0)

    def test_memory_cpu_pids_disk_bounds(self):
        with pytest.raises(ValidationError):
            ResourceBudget(memory_mb=0)
        with pytest.raises(ValidationError):
            ResourceBudget(cpu_count=0)
        with pytest.raises(ValidationError):
            ResourceBudget(pids_limit=0)
        with pytest.raises(ValidationError):
            ResourceBudget(disk_mb=99999)


class TestCodeFile:
    def test_sha256_autocomputed(self):
        f = CodeFile(path="model.py", content="x = 1")
        assert len(f.sha256) == 64

    def test_path_traversal_rejected(self):
        with pytest.raises(ValidationError):
            CodeFile(path="../evil.py", content="x")
        with pytest.raises(ValidationError):
            CodeFile(path="/etc/passwd", content="x")
        with pytest.raises(ValidationError):
            CodeFile(path="a/../../b.py", content="x")


class TestRunRequest:
    def test_valid_request(self):
        req = RunRequest(
            experiment_id=EXP_ID,
            experiment_plan_id=PLAN_ID,
            code_files=files(),
            dependencies=["torch==2.4.1", "numpy==1.26.4"],
        )
        assert req.entrypoint == "train.py"
        assert req.seed == 42

    def test_non_whitelisted_dependency_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(
                experiment_id=EXP_ID,
                experiment_plan_id=PLAN_ID,
                code_files=files(),
                dependencies=["requests==2.0"],
            )

    def test_unpinned_dependency_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(
                experiment_id=EXP_ID,
                experiment_plan_id=PLAN_ID,
                code_files=files(),
                dependencies=["torch"],
            )

    def test_missing_entrypoint_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(
                experiment_id=EXP_ID,
                experiment_plan_id=PLAN_ID,
                code_files=[CodeFile(path="model.py", content="x")],
            )

    def test_empty_code_files_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(experiment_id=EXP_ID, experiment_plan_id=PLAN_ID, code_files=[])


class TestWhitelist:
    def test_exact_pin_required(self):
        assert is_whitelisted("torch==2.4.1")
        assert not is_whitelisted("torch==2.5.0")
        assert not is_whitelisted("torch>=2.4.1")
        assert not is_whitelisted("evil==1.0")


class TestRunResult:
    def test_defaults(self):
        result = RunResult(run_id=uuid4(), status=RunStatus.SUCCEEDED)
        assert result.metrics == {}
        assert result.artifacts == []
        assert result.exit_code is None
        assert result.error_category is None

    def test_error_category_enum(self):
        result = RunResult(
            run_id=uuid4(), status=RunStatus.FAILED, error_category=ErrorCategory.OUT_OF_MEMORY
        )
        assert result.error_category is ErrorCategory.OUT_OF_MEMORY

    def test_audit_defaults(self):
        audit = RunAudit()
        assert audit.image_digest is None
        assert audit.seed is None


class TestMetricsReport:
    def test_valid_report(self):
        report = MetricsReport(
            target_metric="accuracy",
            metric_value=0.97,
            accuracy=0.97,
            epochs_completed=3,
            train_samples=10000,
            test_samples=10000,
            seed=42,
            runtime_seconds=120.0,
        )
        assert report.schema_version == "1.0"

    def test_missing_required_rejected(self):
        with pytest.raises(ValidationError):
            MetricsReport(target_metric="accuracy", metric_value=0.9)

    def test_negative_counts_rejected(self):
        with pytest.raises(ValidationError):
            MetricsReport(
                target_metric="accuracy",
                metric_value=0.9,
                epochs_completed=-1,
                train_samples=10,
                test_samples=10,
                seed=42,
                runtime_seconds=1.0,
            )
