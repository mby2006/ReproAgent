"""Unit tests for the deterministic Reproduction Evaluator.

Covers AST fact extraction from the real generated bundle, all 8 method checks
(matched/deviated/unknown), all 9 experiment checks, metric normalization
(percent/error-rate/pp), the 6-state verdict tree, the 14-section Markdown, and
the /v1/evaluate route. No LLM is involved anywhere (NullLLM raises if called).
"""

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_agent.codegen import generate_from_plan
from reproagent_agent.config import AgentSettings
from reproagent_agent.evaluation import evaluate_run
from reproagent_agent.evaluation.comparison import compare_results
from reproagent_agent.evaluation.experiment import validate_experiment
from reproagent_agent.evaluation.facts import extract_facts
from reproagent_agent.evaluation.markdown import render_markdown
from reproagent_agent.evaluation.method import validate_method
from reproagent_agent.evaluation.verdict import build_summary, decide_final_status
from reproagent_agent.llm.base import LLMAdapter
from reproagent_agent.main import create_app
from reproagent_agent.planning import build_plan
from reproagent_schemas import (
    CheckStatus,
    CodeFile,
    ErrorCategory,
    EvaluateRequest,
    EvidenceField,
    EvidenceStatus,
    ExperimentCheck,
    ExperimentItem,
    FinalStatus,
    MethodCheck,
    MethodItem,
    MetricComparison,
    ReportedResult,
    ReportReferences,
    ReproductionCard,
    RunAudit,
    RunStatus,
)
from reproagent_schemas.common import ConfirmationSource, ReviewDecision, ReviewMode
from reproagent_schemas.plan import Assumption, AssumptionKind, ExperimentPlan, RiskTier

ALL_FINAL = {
    "METHOD_VALIDATED",
    "SCALED_RUN_COMPLETED",
    "PARTIAL_MATCH",
    "DEVIATION_FOUND",
    "INSUFFICIENT_EVIDENCE",
    "EXECUTION_FAILED",
}


class NullLLM(LLMAdapter):
    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        raise AssertionError("evaluation must not call the LLM")


def explicit(value, page=8, quote=None):
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=quote if quote is not None else f"...{value}...",
        source_page=page,
    )


def missing():
    return EvidenceField(value=None, status=EvidenceStatus.MISSING)


def make_card(**overrides) -> ReproductionCard:
    base = dict(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross entropy"),
        hyperparameters={"learning_rate": explicit("0.01")},
        evaluation_metrics=[explicit("accuracy")],
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=0.99,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...test accuracy 99%...",
                source_page=10,
            )
        ],
    )
    base.update(overrides)
    return ReproductionCard(**base)


def make_plan(card=None):
    return build_plan(card or make_card())


def make_facts(card=None):
    plan = make_plan(card)
    return extract_facts(generate_from_plan(plan).files), plan


def full_metrics(plan) -> dict:
    return {
        "accuracy": 0.985,
        "test_loss": 0.05,
        "train_samples": plan.dataset_config.train_subset_size,
        "test_samples": plan.dataset_config.test_subset_size,
        "epochs_completed": plan.training_config.epochs.value,
        "seed": plan.training_config.seed,
        "runtime_seconds": 1.0,
        "metric_value": 0.985,
        "target_metric": "accuracy",
    }


def full_audit(plan) -> RunAudit:
    return RunAudit(
        seed=plan.training_config.seed,
        python_version="3.12.0",
        image_digest="sha256:test",
        dataset_fingerprint="fp-123",
        dependency_lock_hash="lock-123",
        code_version=1,
        plan_version=1,
    )


def make_request(**overrides) -> EvaluateRequest:
    card = overrides.pop("card", None) or make_card()
    plan = overrides.pop("plan", None) or make_plan(card)
    bundle = generate_from_plan(plan)
    base = dict(
        references=ReportReferences(
            paper_id=str(uuid4()),
            spec_version_id=str(uuid4()),
            spec_version=1,
            experiment_id=str(uuid4()),
            plan_id=str(uuid4()),
            plan_version=1,
            code_version=1,
            code_hash=bundle.code_hash,
            run_id=str(uuid4()),
            generated_file_ids=[str(uuid4()) for _ in bundle.files],
        ),
        card=card,
        plan=plan,
        paper_title="LeNet-5 paper",
        code_files=bundle.files,
        run_status=RunStatus.SUCCEEDED,
        metrics=full_metrics(plan),
        audit=full_audit(plan),
        execution_time=1.0,
        exit_code=0,
        artifacts=["metrics.json", "model.pt"],
        stdout_path="r/stdout.txt",
        stderr_path="r/stderr.txt",
    )
    base.update(overrides)
    return EvaluateRequest(**base)


def by_item(checks, item):
    return next(c for c in checks if c.item is item)


# --- AST fact extraction ------------------------------------------------------


def test_extract_facts_from_generated_bundle():
    facts, _ = make_facts()
    assert facts.architecture == "lenet-5"
    assert [c.out_channels for c in facts.conv_layers] == [6, 16]
    assert all(c.kernel_size == 5 for c in facts.conv_layers)
    assert [(layer.in_features, layer.out_features) for layer in facts.linear_layers] == [
        (400, 120),
        (120, 84),
        (84, 10),
    ]
    assert facts.output_features == 10
    assert len(facts.pooling) == 2
    assert all(p.kind == "max" and p.kernel_size == 2 for p in facts.pooling)
    assert facts.activations
    assert facts.loss_function == "cross_entropy"
    assert facts.dataset == "mnist"
    assert "to_tensor" in facts.preprocessing and "normalize" in facts.preprocessing
    assert facts.normalize_mean == pytest.approx(0.1307)
    assert facts.normalize_std == pytest.approx(0.3081)
    assert facts.config["training"]["batch_size"]


def test_extract_facts_survives_syntax_error():
    facts = extract_facts([CodeFile(path="model.py", content="def broken(:")])
    assert facts.model_source is None
    assert facts.conv_layers == []


# --- Method validation --------------------------------------------------------


def test_method_checks_cover_all_eight_items_with_evidence():
    card = make_card()
    facts, _ = make_facts(card)
    checks = validate_method(card, facts)
    assert [c.item for c in checks] == list(MethodItem)
    for item in (MethodItem.ARCHITECTURE, MethodItem.LOSS_FUNCTION, MethodItem.DATASET):
        check = by_item(checks, item)
        assert check.status is CheckStatus.MATCHED
        assert check.paper_evidence.kind == "paper"
        assert check.paper_evidence.source_page >= 1
        assert check.implementation_evidence.kind == "code"
        assert check.implementation_evidence.file_path
    # items without paper evidence must be unknown, never guessed
    for item in (
        MethodItem.LAYER_CONFIGURATION,
        MethodItem.ACTIVATION,
        MethodItem.SUBSAMPLING,
        MethodItem.OUTPUT_LAYER,
        MethodItem.PREPROCESSING,
    ):
        assert by_item(checks, item).status is CheckStatus.UNKNOWN


def test_method_layers_matched_and_deviated():
    card = make_card(
        model_architecture=explicit(
            "LeNet-5", quote="C1 has 6 feature maps and C3 has 16 feature maps."
        )
    )
    facts, _ = make_facts(card)
    assert by_item(validate_method(card, facts), MethodItem.LAYER_CONFIGURATION).status is (
        CheckStatus.MATCHED
    )
    wrong = make_card(model_architecture=explicit("LeNet-5", quote="C1 has 32 feature maps."))
    check = by_item(validate_method(wrong, facts), MethodItem.LAYER_CONFIGURATION)
    assert check.status is CheckStatus.DEVIATED


def test_method_activation_deviated_when_paper_says_tanh():
    card = make_card(
        model_architecture=explicit("LeNet-5", quote="LeNet-5 uses tanh activation.")
    )
    facts, _ = make_facts(card)
    facts = facts.model_copy(update={"activations": ["relu"]})
    check = by_item(validate_method(card, facts), MethodItem.ACTIVATION)
    assert check.status is CheckStatus.DEVIATED
    assert "tanh" in check.paper_value and "relu" in check.implementation_value


def test_method_subsampling_explicit_max_pool_matched():
    card = make_card(
        model_architecture=explicit("LeNet-5", quote="LeNet-5 with max pooling layers.")
    )
    facts, _ = make_facts(card)
    assert by_item(validate_method(card, facts), MethodItem.SUBSAMPLING).status is (
        CheckStatus.MATCHED
    )


def test_method_output_layer_deviated():
    card = make_card(
        model_architecture=explicit("LeNet-5", quote="The output layer has 100 units.")
    )
    facts, _ = make_facts(card)
    check = by_item(validate_method(card, facts), MethodItem.OUTPUT_LAYER)
    assert check.status is CheckStatus.DEVIATED


def test_method_missing_evidence_is_unknown():
    card = make_card(
        method=missing(),
        model_architecture=missing(),
        loss_function=missing(),
        dataset=missing(),
    )
    facts, _ = make_facts()  # facts from the canonical pinned code; card lacks evidence
    checks = validate_method(card, facts)
    for item in (MethodItem.ARCHITECTURE, MethodItem.LOSS_FUNCTION, MethodItem.DATASET):
        check = by_item(checks, item)
        assert check.status is CheckStatus.UNKNOWN
        assert check.paper_evidence is None


def test_method_preprocessing_deviated_on_augmentation():
    card = make_card(
        data_preprocessing={"augmentation": explicit("random crop and horizontal flip")}
    )
    facts, _ = make_facts(card)
    check = by_item(validate_method(card, facts), MethodItem.PREPROCESSING)
    assert check.status is CheckStatus.DEVIATED


def test_method_dataset_deviated_on_cifar():
    card = make_card(dataset=explicit("CIFAR-10"))
    facts, _ = make_facts()  # planner only supports MNIST; facts stay the pinned MNIST code
    assert by_item(validate_method(card, facts), MethodItem.DATASET).status is (
        CheckStatus.DEVIATED
    )


# --- Experiment validation ----------------------------------------------------


def test_experiment_checks_all_nine_matched():
    card = make_card()
    facts, plan = make_facts(card)
    checks = validate_experiment(plan, facts, full_metrics(plan), full_audit(plan), 1.0)
    assert [c.item for c in checks] == list(ExperimentItem)
    assert all(c.status is CheckStatus.MATCHED for c in checks), [
        (c.item.value, c.note) for c in checks if c.status is not CheckStatus.MATCHED
    ]
    assert by_item(checks, ExperimentItem.TRAINING_SAMPLES).actual_evidence.kind == "metrics"
    assert by_item(checks, ExperimentItem.BATCH_SIZE).actual_evidence.kind == "config"
    assert by_item(checks, ExperimentItem.DATASET_FINGERPRINT).actual_evidence.kind == "audit"


def test_experiment_seed_mismatch_deviated():
    card = make_card()
    facts, plan = make_facts(card)
    metrics = full_metrics(plan) | {"seed": 7}
    checks = validate_experiment(plan, facts, metrics, full_audit(plan), 1.0)
    assert by_item(checks, ExperimentItem.RANDOM_SEED).status is CheckStatus.DEVIATED


def test_experiment_over_budget_runtime_deviated():
    card = make_card()
    facts, plan = make_facts(card)
    checks = validate_experiment(
        plan, facts, full_metrics(plan), full_audit(plan), plan.runtime_budget.max_seconds + 1
    )
    assert by_item(checks, ExperimentItem.RUNTIME_ENVIRONMENT).status is CheckStatus.DEVIATED


def test_experiment_missing_run_data_unknown():
    card = make_card()
    facts, plan = make_facts(card)
    checks = validate_experiment(plan, facts, {}, None, None)
    for item in (
        ExperimentItem.TRAINING_SAMPLES,
        ExperimentItem.EPOCHS,
        ExperimentItem.RANDOM_SEED,
        ExperimentItem.DATASET_FINGERPRINT,
        ExperimentItem.RUNTIME_ENVIRONMENT,
    ):
        assert by_item(checks, item).status is CheckStatus.UNKNOWN


def test_experiment_epochs_mismatch_deviated():
    card = make_card()
    facts, plan = make_facts(card)
    metrics = full_metrics(plan) | {"epochs_completed": plan.training_config.epochs.value + 3}
    checks = validate_experiment(plan, facts, metrics, full_audit(plan), 1.0)
    assert by_item(checks, ExperimentItem.EPOCHS).status is CheckStatus.DEVIATED


# --- Result comparison --------------------------------------------------------


def test_comparison_percent_normalization_and_pp():
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=99.2,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...99.2%...",
                source_page=10,
            )
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.98}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.paper_value == pytest.approx(0.992)
    assert acc.experiment_value == pytest.approx(0.98)
    assert acc.absolute_difference == pytest.approx(0.012)
    assert acc.percentage_point_difference == pytest.approx(-1.2)
    assert acc.relative_difference == pytest.approx(0.012 / 0.992)
    assert acc.unit == "fraction"
    assert acc.comparison_status is CheckStatus.MATCHED  # 0.012 <= 0.03 trend tolerance
    assert acc.paper_evidence.kind == "paper"
    assert acc.experiment_evidence.kind == "metrics"


def test_comparison_error_rate_converted_to_accuracy():
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="error rate",
                value=0.8,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...error rate 0.8 percent...",
                source_page=10,
            )
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.99}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.paper_value == pytest.approx(0.992)
    assert "错误率" in acc.note


def test_comparison_ambiguous_error_rate_is_not_normalized():
    # "0.8" without a percent sign could be 0.8% or 80% error. Normalizing it to
    # a fraction would make paper accuracy 0.2, falsely matching a bad run.
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="error rate",
                value=0.8,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="LeNet-5 | 0.8",
                source_page=10,
            )
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.2}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.paper_value is None
    assert acc.comparison_status is CheckStatus.UNKNOWN
    assert "未标注百分号" in acc.note
    assert acc.experiment_value == pytest.approx(0.2)


def test_comparison_error_rate_above_one_assumed_percent():
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="test error rate",
                value=1.7,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="error 1.7",
                source_page=10,
            )
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.98}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.paper_value == pytest.approx(0.983)


def test_comparison_ambiguous_error_alongside_clear_accuracy():
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=99.2,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...99.2%...",
                source_page=10,
            ),
            ReportedResult(
                metric="error rate",
                value=0.8,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="LeNet-5 | 0.8",
                source_page=10,
            ),
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.99}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.paper_value == pytest.approx(0.992)
    ambig = next(c for c in rows if c.metric == "accuracy_ambiguous")
    assert ambig.paper_value is None
    assert ambig.comparison_status is CheckStatus.UNKNOWN


def test_comparison_without_paper_value_not_applicable():
    card = make_card(reported_results=[])
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.9}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.comparison_status is CheckStatus.NOT_APPLICABLE
    assert acc.paper_value is None
    assert acc.experiment_value == pytest.approx(0.9)
    assert acc.absolute_difference is None


def test_comparison_failed_run_has_no_experiment_value():
    card = make_card()
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.9}, RunStatus.FAILED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.experiment_value is None
    assert acc.comparison_status is CheckStatus.UNKNOWN


def test_comparison_outside_tolerance_deviated_but_not_failure():
    card = make_card()
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.8}, RunStatus.SUCCEEDED)
    acc = next(c for c in rows if c.metric == "accuracy")
    assert acc.comparison_status is CheckStatus.DEVIATED
    assert "不构成复现失败定论" in acc.note


def test_comparison_loss_records_diffs_without_acceptance():
    card = make_card(
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=0.99,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...accuracy 0.99...",
                source_page=10,
            ),
            ReportedResult(
                metric="test loss",
                value=0.03,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...test loss 0.03...",
                source_page=10,
            ),
        ]
    )
    plan = make_plan(card)
    rows = compare_results(card, plan, {"accuracy": 0.985, "test_loss": 0.1}, RunStatus.SUCCEEDED)
    loss = next(c for c in rows if c.metric == "test_loss")
    assert loss.absolute_difference == pytest.approx(0.07)
    assert loss.comparison_status is CheckStatus.UNKNOWN
    assert loss.tolerance is None
    assert loss.percentage_point_difference is None


# --- Verdict decision tree ----------------------------------------------------


def mc(item, status):
    return MethodCheck(item=item, status=status, note="test")


def ec(item, status):
    return ExperimentCheck(item=item, status=status, note="test")


def comp(metric, status, paper=0.99, experiment=0.985):
    abs_diff = rel_diff = pp_diff = None
    if paper is not None and experiment is not None:
        abs_diff = abs(experiment - paper)
        rel_diff = abs_diff / abs(paper) if paper != 0 else None
        if metric.startswith("accuracy"):
            pp_diff = (experiment - paper) * 100.0
    return MetricComparison(
        metric=metric,
        unit="fraction",
        comparison_status=status,
        paper_value=paper,
        experiment_value=experiment,
        absolute_difference=abs_diff,
        relative_difference=rel_diff,
        percentage_point_difference=pp_diff,
    )


def critical_matched():
    return [
        mc(MethodItem.ARCHITECTURE, CheckStatus.MATCHED),
        mc(MethodItem.LOSS_FUNCTION, CheckStatus.MATCHED),
        mc(MethodItem.DATASET, CheckStatus.MATCHED),
    ]


def test_verdict_execution_failed():
    assert decide_final_status(
        RunStatus.FAILED, critical_matched(), [], [comp("accuracy", CheckStatus.MATCHED)]
    ) is FinalStatus.EXECUTION_FAILED


def test_verdict_deviation_found_wins_over_match():
    checks = critical_matched() + [mc(MethodItem.ACTIVATION, CheckStatus.DEVIATED)]
    assert decide_final_status(
        RunStatus.SUCCEEDED, checks, [], [comp("accuracy", CheckStatus.MATCHED)]
    ) is FinalStatus.DEVIATION_FOUND


def test_verdict_insufficient_without_paper_value():
    rows = [comp("accuracy", CheckStatus.NOT_APPLICABLE, paper=None)]
    assert decide_final_status(
        RunStatus.SUCCEEDED, critical_matched(), [], rows
    ) is FinalStatus.INSUFFICIENT_EVIDENCE


def test_verdict_insufficient_when_critical_unknown():
    checks = [
        mc(MethodItem.ARCHITECTURE, CheckStatus.UNKNOWN),
        mc(MethodItem.LOSS_FUNCTION, CheckStatus.MATCHED),
        mc(MethodItem.DATASET, CheckStatus.MATCHED),
    ]
    assert decide_final_status(
        RunStatus.SUCCEEDED, checks, [], [comp("accuracy", CheckStatus.MATCHED)]
    ) is FinalStatus.INSUFFICIENT_EVIDENCE


def test_verdict_partial_match():
    rows = [
        comp("accuracy", CheckStatus.MATCHED),
        comp("accuracy_2", CheckStatus.DEVIATED, experiment=0.5),
    ]
    assert decide_final_status(
        RunStatus.SUCCEEDED, critical_matched(), [], rows
    ) is FinalStatus.PARTIAL_MATCH


def test_verdict_method_validated():
    assert decide_final_status(
        RunStatus.SUCCEEDED, critical_matched(), [], [comp("accuracy", CheckStatus.MATCHED)]
    ) is FinalStatus.METHOD_VALIDATED


def test_verdict_scaled_run_completed():
    rows = [comp("accuracy", CheckStatus.DEVIATED, experiment=0.5)]
    assert decide_final_status(
        RunStatus.SUCCEEDED, critical_matched(), [], rows
    ) is FinalStatus.SCALED_RUN_COMPLETED


@pytest.mark.parametrize("status", list(FinalStatus))
def test_summary_always_disclaims_full_reproduction(status):
    summary = build_summary(status, [comp("accuracy", CheckStatus.MATCHED)], [], [])
    assert "Full Reproduction" in summary
    assert "不构成" in summary
    assert status.value in ALL_FINAL


# --- End-to-end evaluator + Markdown ------------------------------------------


def test_evaluate_method_validated_end_to_end():
    request = make_request()
    response = evaluate_run(request)
    report = response.report
    assert report.final_status is FinalStatus.METHOD_VALIDATED
    assert report.references == request.references
    assert len(report.method_checks) == 8
    assert len(report.experiment_checks) == 9
    assert report.scaling_deviations == request.plan.deviations_from_paper
    assert report.assumptions == request.plan.assumptions
    assert any("Full Reproduction" in lim for lim in report.limitations)
    acc = next(c for c in report.metric_comparisons if c.metric == "accuracy")
    assert acc.experiment_value == pytest.approx(0.985)
    assert acc.paper_value == pytest.approx(0.99)


def test_evaluate_failed_run_execution_failed():
    request = make_request(
        run_status=RunStatus.FAILED,
        exit_code=1,
        error="ModuleNotFoundError: xyz",
        error_category=ErrorCategory.IMPORT_ERROR,
        metrics={},
    )
    response = evaluate_run(request)
    assert response.report.final_status is FinalStatus.EXECUTION_FAILED
    assert "ModuleNotFoundError" in response.markdown


def test_evaluate_no_paper_results_insufficient():
    request = make_request(card=make_card(reported_results=[]))
    assert evaluate_run(request).report.final_status is FinalStatus.INSUFFICIENT_EVIDENCE


def test_markdown_has_exactly_fourteen_sections_in_order():
    request = make_request()
    response = evaluate_run(request)
    numbers = [int(m) for m in re.findall(r"^## (\d+)\. ", response.markdown, re.MULTILINE)]
    assert numbers == list(range(1, 15))
    assert "Paper Overview" in response.markdown
    assert "Code and Execution Trace References" in response.markdown
    assert str(request.references.run_id) in response.markdown
    assert str(request.references.plan_id) in response.markdown
    assert "r/stdout.txt" in response.markdown


def test_markdown_escapes_pipes_in_evidence():
    card = make_card(method=explicit("LeNet-5", quote="a|b|c"))
    request = make_request(card=card)
    markdown = evaluate_run(request).markdown
    assert "a\\|b\\|c" in markdown


def test_markdown_render_matches_report():
    request = make_request()
    response = evaluate_run(request)
    facts = extract_facts(request.code_files)
    # SchemaModel strips whitespace on str fields, so compare stripped renders
    assert render_markdown(request, response.report, facts).strip() == response.markdown


# --- Route --------------------------------------------------------------------


def make_client() -> TestClient:
    return TestClient(create_app(AgentSettings(_env_file=None), llm=NullLLM()))


def test_evaluate_route_returns_report_and_markdown():
    request = make_request()
    with make_client() as client:
        response = client.post("/v1/evaluate", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["final_status"] in ALL_FINAL
    assert body["report"]["references"]["run_id"] == str(request.references.run_id)
    assert len(re.findall(r"^## \d+\. ", body["markdown"], re.MULTILINE)) == 14


def test_evaluate_route_rejects_invalid_body():
    with make_client() as client:
        response = client.post("/v1/evaluate", json={"references": {}})
    assert response.status_code == 422


# --- Review provenance, disclaimers and immutable audit references -------------

DELEGATED_NOTICE = "本实验关键假设由系统自动接受，未经人工逐项核验；结果仅供趋势参考。"


def review_assumption(source=None, **kwargs):
    return Assumption(
        field_path="training_config.batch_size", kind=AssumptionKind.INFERRED,
        value=64, rationale="routine choice", risk_tier=RiskTier.ROUTINE,
        human_confirmed=source in (ConfirmationSource.HUMAN, ConfirmationSource.AI_ASSISTED),
        confirmed_by=source, **kwargs,
    )


def review_event(source=ConfirmationSource.AUTO):
    return ReviewDecision(
        field_path="training_config.batch_size", before={"value": 32}, after={"value": 64},
        confirmed_by=source, actor=uuid4(), review_mode=ReviewMode.DELEGATED,
        timestamp=datetime(2026, 1, 2, tzinfo=UTC), recommendation_id="recommendation-1",
        recommendation_reasoning="safe|routine", recommendation_confidence=0.95,
    )


def test_delegated_report_warns_early_and_preserves_actual_authorization_and_refs():
    plan = make_plan()
    stamp, actor = datetime(2026, 1, 2, tzinfo=UTC), uuid4()
    plan = ExperimentPlan.model_validate(plan.model_dump() | {
        "review_mode": "delegated", "delegated_at": stamp, "delegated_by": actor,
        "assumptions": [review_assumption(ConfirmationSource.AUTO)],
    })
    decision = review_event()
    request = make_request(plan=plan, review_decisions=[decision, decision])
    response = evaluate_run(request)
    report = response.report
    assert report.review_mode is ReviewMode.DELEGATED
    assert report.delegated_at == stamp and report.delegated_by == actor
    assert report.review_disclaimer == DELEGATED_NOTICE
    assert report.review_decisions == [decision]
    assert report.references.review_trace_ids == [decision.id]
    assert request.references.review_trace_ids == []
    assert response.markdown.index(DELEGATED_NOTICE) < response.markdown.index("## 1.")
    assert f"{actor}" in response.markdown and stamp.isoformat() in response.markdown
    assert f"#review-{decision.id}" in response.markdown
    assert f'id="review-{decision.id}"' in response.markdown
    assert "safe\\|routine" in response.markdown
    for text in ("old", "new", "actor", "time", "source", "auto（系统自动接受，非人工确认）"):
        assert text in response.markdown


@pytest.mark.parametrize("mode", [ReviewMode.EXPERT, ReviewMode.ASSISTED])
def test_past_auto_acceptance_remains_disclaimed_after_mode_switch(mode):
    plan = make_plan()
    plan.review_mode = mode
    plan.assumptions = [review_assumption(ConfirmationSource.HUMAN)]
    response = evaluate_run(make_request(plan=plan, review_decisions=[review_event()]))
    assert response.report.review_mode is mode
    assert response.report.review_disclaimer == DELEGATED_NOTICE
    assert response.report.delegated_at is None and response.report.delegated_by is None


def test_expert_with_unconfirmed_routine_never_claims_itemwise_human_review():
    plan = make_plan()
    plan.review_mode = ReviewMode.EXPERT
    plan.assumptions = [review_assumption()]
    response = evaluate_run(make_request(plan=plan, review_decisions=[
        review_event(ConfirmationSource.HUMAN),
    ]))
    assert "未经人工确认" in response.report.review_disclaimer
    assert "不能认定已人工逐项核验" in response.markdown
    assert "未确认" in response.markdown


@pytest.mark.parametrize("source,label", [
    (ConfirmationSource.HUMAN, "human（人工确认）"),
    (ConfirmationSource.AI_ASSISTED, "ai_assisted（AI 辅助，人工采纳）"),
    (ConfirmationSource.AUTO, "auto（系统自动接受，非人工确认）"),
    (None, "未确认"),
])
def test_assumptions_keep_their_confirmation_source(source, label):
    plan = make_plan()
    plan.assumptions = [review_assumption(source)]
    response = evaluate_run(make_request(plan=plan))
    assert response.report.assumptions[0].confirmed_by is source
    assert label in response.markdown
    if source is ConfirmationSource.AUTO:
        assert not response.report.assumptions[0].human_confirmed
        assert response.report.review_disclaimer == DELEGATED_NOTICE


def test_legacy_report_is_honestly_unknown_without_synthetic_history():
    raw = make_plan().model_dump()
    for field in ("review_mode", "delegated_at", "delegated_by"):
        raw.pop(field, None)
    plan = ExperimentPlan.model_validate(raw)
    response = evaluate_run(make_request(plan=plan))
    assert response.report.review_mode is None
    assert response.report.review_decisions == []
    assert response.report.references.review_trace_ids == []
    assert "unknown" in response.report.review_disclaimer
    assert "审核来源未知" in response.markdown


@pytest.mark.parametrize("source", list(ConfirmationSource))
def test_inferred_result_is_not_upgraded_to_paper_evidence_by_acceptance(source):
    card = make_card(reported_results=[ReportedResult(
        metric="accuracy", value=0.999, status=EvidenceStatus.INFERRED,
        human_confirmed=source is not ConfirmationSource.AUTO, confirmed_by=source,
        evidence_quote="suggested accuracy 99.9%", source_page=10,
    )])
    response = evaluate_run(make_request(card=card))
    acc = next(c for c in response.report.metric_comparisons if c.metric == "accuracy")
    assert acc.paper_value is None and acc.paper_evidence is None
    assert acc.comparison_status is CheckStatus.NOT_APPLICABLE
    assert response.report.final_status is FinalStatus.INSUFFICIENT_EVIDENCE
    assert "论文报告值：0.999" not in response.markdown


@pytest.mark.parametrize("source", [ConfirmationSource.AUTO, ConfirmationSource.AI_ASSISTED])
def test_inferred_method_evidence_cannot_become_paper_facts(source):
    card = make_card()
    for name in ("method", "model_architecture", "dataset", "loss_function"):
        field = getattr(card, name)
        setattr(card, name, EvidenceField(
            value=field.value, status=EvidenceStatus.INFERRED,
            confirmed_by=source, human_confirmed=source is ConfirmationSource.AI_ASSISTED,
            evidence_quote="LeNet-5 with 6 feature maps and max pooling", source_page=10,
        ))
    facts, _ = make_facts()
    checks = validate_method(card, facts)
    assert all(c.status is CheckStatus.UNKNOWN for c in checks)
    assert all(c.paper_evidence is None and c.paper_value is None for c in checks)
