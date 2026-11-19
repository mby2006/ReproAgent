"""Deterministic Reproduction Evaluator: no LLM anywhere in the judgment path."""

from reproagent_schemas import (
    CheckStatus,
    EvaluateRequest,
    EvaluateResponse,
    EvaluationReport,
)

from .comparison import compare_results
from .experiment import validate_experiment
from .facts import extract_facts
from .markdown import render_markdown
from .method import validate_method
from .verdict import build_summary, decide_final_status


def _collect_missing(request: EvaluateRequest, report_kwargs: dict) -> list[str]:
    missing = list(request.card.missing_information)
    for check in report_kwargs["method_checks"]:
        if check.status is CheckStatus.UNKNOWN:
            missing.append(f"方法验证/{check.item.value}：{check.note}")
    for check in report_kwargs["experiment_checks"]:
        if check.status is CheckStatus.UNKNOWN:
            missing.append(f"实验验证/{check.item.value}：{check.note}")
    for comparison in report_kwargs["metric_comparisons"]:
        if comparison.paper_value is None:
            missing.append(f"结果对比/{comparison.metric}：论文证据未报告该指标")
    return missing


def _limitations(request: EvaluateRequest) -> list[str]:
    plan = request.plan
    dc = plan.dataset_config
    return [
        f"缩放实验：训练子集 {dc.train_subset_size}/{dc.full_train_size}、"
        f"测试子集 {dc.test_subset_size}/{dc.full_test_size}、"
        f"{plan.training_config.epochs.value} epoch，非论文全量训练；"
        "结果不能与论文全量结果直接等价。",
        f"tolerance={plan.evaluation_config.tolerance} 仅作趋势性参考，非论文等价判定标准。",
        "CPU 隔离容器执行，运行时长与论文硬件环境不可直接对比。",
        "确定性代码模板仅覆盖 LeNet-5 + MNIST；其他方法/数据集不在 v1 验证范围。",
        "本报告任何状态都不构成完整复现（Full Reproduction）结论。",
    ]


def evaluate_run(request: EvaluateRequest) -> EvaluateResponse:
    facts = extract_facts(request.code_files)
    method_checks = validate_method(request.card, facts)
    experiment_checks = validate_experiment(
        request.plan, facts, request.metrics, request.audit, request.execution_time
    )
    comparisons = compare_results(request.card, request.plan, request.metrics, request.run_status)
    target = request.plan.evaluation_config.target_metric.strip().lower().replace("_", " ")
    target_key = (
        "accuracy" if target in ("accuracy", "test accuracy", "error", "error rate") else target
    )
    status = decide_final_status(
        request.run_status, method_checks, experiment_checks, comparisons, target_key
    )
    summary = build_summary(status, comparisons, method_checks, experiment_checks)
    parts = {
        "method_checks": method_checks,
        "experiment_checks": experiment_checks,
        "metric_comparisons": comparisons,
    }
    report = EvaluationReport(
        references=request.references,
        final_status=status,
        summary=summary,
        scaling_deviations=request.plan.deviations_from_paper,
        missing_information=_collect_missing(request, parts),
        assumptions=request.plan.assumptions,
        limitations=_limitations(request),
        **parts,
    )
    return EvaluateResponse(report=report, markdown=render_markdown(request, report, facts))