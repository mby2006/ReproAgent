"""Integration: real locked-down container execution via the Docker sandbox.

Requires a running Docker daemon and the baked `reproagent-exec:latest` image
(scripts/build_exec_image.sh). These tests spawn REAL sibling containers and
verify the acceptance criteria: genuine training + metrics, hard timeout kill,
memory limit, network isolation, absence of host credentials, container
destruction, and one real error detection + auto-repair cycle.

Run explicitly:  uv run pytest tests/integration/test_execution.py -q
"""

import threading
from pathlib import Path
from uuid import uuid4

import pytest
from reproagent_agent.codegen import (
    classify_error,
    generate_from_plan,
    repair_bundle,
    validate_bundle,
)
from reproagent_agent.evaluation import evaluate_run
from reproagent_agent.planning import build_plan
from reproagent_api.services.plan_edits import apply_plan_edits
from reproagent_runner.config import RunnerSettings
from reproagent_runner.sandbox import Sandbox, cleanup_run_dir, make_client
from reproagent_schemas import (
    CheckStatus,
    ClassifyRequest,
    CodeBundle,
    CodeFile,
    ErrorCategory,
    EvaluateRequest,
    EvidenceField,
    EvidenceStatus,
    ExperimentItem,
    FinalStatus,
    MethodItem,
    PlanChange,
    PlanEditRequest,
    RepairRequest,
    ReportedResult,
    ReportReferences,
    ReproductionCard,
    ResourceBudget,
    RunRequest,
    RunStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = REPO_ROOT / ".runner-work-it"
DEPS = ["numpy==1.26.4", "pyyaml==6.0.2", "torch==2.4.1", "torchvision==0.19.1"]


def make_card() -> ReproductionCard:
    def explicit(value, page=8):
        return EvidenceField(
            value=value,
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote=f"...{value}...",
            source_page=page,
        )

    return ReproductionCard(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross entropy"),
        hyperparameters={
            "learning_rate": explicit("0.01"),
            "batch_size": explicit(64, page=9),
            "epochs": explicit(2, page=9),
        },
        evaluation_metrics=[explicit("accuracy")],
    )


def small_plan():
    plan = build_plan(make_card())
    edit = PlanEditRequest(
        base_version=1,
        changes=[
            PlanChange(field_path="dataset_config.train_subset_size", value=256),
            PlanChange(field_path="dataset_config.test_subset_size", value=128),
            PlanChange(field_path="training_config.epochs", value=1),
        ],
        change_reason="integration: tiny subset for fast real training",
    )
    new_plan, _ = apply_plan_edits(plan, edit)
    return new_plan


@pytest.fixture(scope="module")
def settings() -> RunnerSettings:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return RunnerSettings(work_dir=WORK_DIR, enforce_disk_quota=False, _env_file=None)


@pytest.fixture(scope="module")
def client(settings):
    try:
        c = make_client(settings)
        c.ping()
        c.images.get(settings.exec_image)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Docker daemon or exec image unavailable: {exc}")
    return c


@pytest.fixture(scope="module")
def sandbox(client, settings) -> Sandbox:
    return Sandbox(client, settings, host_work_dir=WORK_DIR.as_posix())


def execute(sandbox, settings, files, budget=None, deps=None):
    run_dir = settings.work_dir / str(uuid4())
    request = RunRequest(
        experiment_id=uuid4(),
        experiment_plan_id=uuid4(),
        code_files=files,
        dependencies=deps or DEPS,
        resource_budget=budget or ResourceBudget(),
        entrypoint="train.py",
    )
    captured: list[str] = []
    result = sandbox.execute(request, run_dir, threading.Event(), on_container=captured.append)
    cleanup_run_dir(run_dir)
    return result, captured


def raw_files(content: str) -> list[CodeFile]:
    return [CodeFile(path="train.py", content=content)]


def container_gone(client, captured) -> bool:
    if not captured:
        return False
    ids = {c.id for c in client.containers.list(all=True)}
    return captured[0] not in ids


def test_real_training_produces_metrics_and_audit(sandbox, settings, client):
    bundle = generate_from_plan(small_plan())
    budget = ResourceBudget(max_seconds=180, memory_mb=1024)
    result, captured = execute(sandbox, settings, bundle.files, budget, bundle.requirements)
    assert result.status is RunStatus.SUCCEEDED, result.stderr
    assert result.exit_code == 0
    assert result.metrics.get("target_metric") == "accuracy"
    assert isinstance(result.metrics.get("metric_value"), (int, float))
    assert "metrics.json" in result.artifacts
    assert "model.pt" in result.artifacts
    audit = result.audit
    assert audit.image_digest
    assert audit.python_version and audit.python_version.startswith("3.12")
    assert audit.dataset_fingerprint
    assert audit.dependency_lock_hash
    assert audit.seed == 42
    assert container_gone(client, captured)


def test_hard_timeout_kills_container(sandbox, settings, client):
    files = raw_files("import time\ntime.sleep(120)\n")
    budget = ResourceBudget(max_seconds=3, memory_mb=256)
    result, captured = execute(sandbox, settings, files, budget)
    assert result.status is RunStatus.TIMEOUT
    assert result.error_category is ErrorCategory.TIMEOUT
    assert result.execution_time < 20
    assert container_gone(client, captured)


def test_memory_limit_kills_process(sandbox, settings):
    files = raw_files("x = bytearray(512 * 1024 * 1024)\nprint(len(x))\n")
    budget = ResourceBudget(max_seconds=30, memory_mb=128)
    result, _ = execute(sandbox, settings, files, budget)
    assert result.status is RunStatus.FAILED
    assert result.exit_code in (137, -9)


def test_network_isolation(sandbox, settings):
    files = raw_files(
        "import socket\nsocket.create_connection(('example.com', 80), timeout=5)\n"
    )
    result, _ = execute(sandbox, settings, files, ResourceBudget(max_seconds=30))
    assert result.status is RunStatus.FAILED
    assert result.exit_code != 0


def test_no_host_credentials_in_environment(sandbox, settings):
    files = raw_files("import os, json\nprint(json.dumps(dict(os.environ)))\n")
    result, _ = execute(sandbox, settings, files, ResourceBudget(max_seconds=30))
    blob = result.stdout + result.stderr
    for secret in ("QWEN_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE", "sk-"):
        assert secret not in blob


def test_cannot_read_unmounted_host_files(sandbox, settings):
    files = raw_files("open('/host-secret.txt').read()\n")
    result, _ = execute(sandbox, settings, files, ResourceBudget(max_seconds=30))
    assert result.status is RunStatus.FAILED
    assert result.exit_code != 0


def test_real_error_detection_and_auto_repair(sandbox, settings):
    plan = small_plan()
    good = generate_from_plan(plan)
    broken = CodeBundle(
        files=[
            CodeFile(path="train.py", content="import nonexistent_module_xyz\n"),
            *[f for f in good.files if f.path != "train.py"],
        ],
        entrypoint="train.py",
        requirements=good.requirements,
    )
    budget = ResourceBudget(max_seconds=180, memory_mb=1024)

    first, _ = execute(sandbox, settings, broken.files, budget, broken.requirements)
    assert first.status is RunStatus.FAILED
    assert "ModuleNotFoundError" in first.stderr

    classification = classify_error(
        ClassifyRequest(
            stderr=first.stderr,
            stdout=first.stdout,
            exit_code=first.exit_code,
            status=first.status,
        )
    )
    assert classification.category is ErrorCategory.IMPORT_ERROR
    assert classification.repairable

    repair = repair_bundle(
        RepairRequest(bundle=broken, classification=classification, plan=plan)
    )
    assert not repair.replan_required
    report = validate_bundle(repair.bundle)
    assert report.ok, report.issues

    second, _ = execute(sandbox, settings, repair.bundle.files, budget, repair.bundle.requirements)
    assert second.status is RunStatus.SUCCEEDED, second.stderr
    assert second.metrics.get("metric_value") is not None


def test_evaluation_report_from_real_run(sandbox, settings):
    """Phase C: evaluate a REAL container run end-to-end (no LLM, no fabrication)."""
    import re

    plan = small_plan()
    bundle = generate_from_plan(plan)
    budget = ResourceBudget(max_seconds=180, memory_mb=1024)
    result, _ = execute(sandbox, settings, bundle.files, budget, bundle.requirements)
    assert result.status is RunStatus.SUCCEEDED, result.stderr

    card = make_card().model_copy(
        update={
            "reported_results": [
                ReportedResult(
                    metric="error rate",
                    value=0.8,
                    status=EvidenceStatus.PAPER_EXPLICIT,
                    evidence_quote="...the error rate on the MNIST test set is 0.8 percent...",
                    source_page=10,
                )
            ]
        }
    )
    request = EvaluateRequest(
        references=ReportReferences(
            paper_id=str(uuid4()),
            spec_version_id=str(uuid4()),
            spec_version=1,
            experiment_id=str(uuid4()),
            plan_id=str(uuid4()),
            plan_version=1,
            code_version=1,
            code_hash=bundle.code_hash,
            run_id=str(result.run_id),
            generated_file_ids=[],
        ),
        card=card,
        plan=plan,
        paper_title="LeNet-5 (integration)",
        code_files=bundle.files,
        run_status=result.status,
        metrics=result.metrics,
        audit=result.audit,
        execution_time=result.execution_time,
        exit_code=result.exit_code,
        artifacts=result.artifacts,
    )
    response = evaluate_run(request)
    report = response.report

    assert report.final_status in set(FinalStatus)
    assert report.final_status is not FinalStatus.EXECUTION_FAILED
    assert "Full Reproduction" in report.summary
    assert report.references.run_id == result.run_id

    # experiment checks reflect what the container ACTUALLY executed
    exp = {c.item: c for c in report.experiment_checks}
    assert exp[ExperimentItem.TRAINING_SAMPLES].actual_value == 256
    assert exp[ExperimentItem.TRAINING_SAMPLES].status is CheckStatus.MATCHED
    assert exp[ExperimentItem.TEST_SAMPLES].actual_value == 128
    assert exp[ExperimentItem.EPOCHS].actual_value == 1
    assert exp[ExperimentItem.RANDOM_SEED].status is CheckStatus.MATCHED
    assert exp[ExperimentItem.DATASET_FINGERPRINT].status is CheckStatus.MATCHED
    assert exp[ExperimentItem.RUNTIME_ENVIRONMENT].status is CheckStatus.MATCHED

    # critical method items matched against the pinned real code
    method = {c.item: c for c in report.method_checks}
    for item in (MethodItem.ARCHITECTURE, MethodItem.LOSS_FUNCTION, MethodItem.DATASET):
        assert method[item].status is CheckStatus.MATCHED

    # metric comparison grounded in real metrics + evidence-converted paper value
    acc = next(c for c in report.metric_comparisons if c.metric == "accuracy")
    assert acc.experiment_value == result.metrics["accuracy"]
    assert acc.paper_value == pytest.approx(0.992)
    assert acc.percentage_point_difference is not None

    sections = [int(m) for m in re.findall(r"^## (\d+)\. ", response.markdown, re.MULTILINE)]
    assert sections == list(range(1, 15))