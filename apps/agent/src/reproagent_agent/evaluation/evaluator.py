"""Deterministic Reproduction Evaluator: no LLM anywhere in the judgment path."""

from reproagent_schemas import (
    CheckStatus,
    EvaluateRequest,
    EvaluateResponse,
    EvaluationReport,
)
from reproagent_schemas.common import ConfirmationSource, ReviewMode
from reproagent_schemas.plan import execution_metric

from .comparison import compare_results
from .experiment import validate_experiment
from .facts import extract_facts
from .markdown import render_markdown
from .method import validate_method
from .parameters import parameter_comparisons
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
        f"缩放计划：训练子集 {dc.train_subset_size}/{dc.full_train_size}、"
        f"测试子集 {dc.test_subset_size}/{dc.full_test_size}、"
        f"{plan.training_config.epochs.value} epoch，非论文全量训练；"
        "结果不能与论文全量结果直接等价。",
        f"tolerance={plan.evaluation_config.tolerance} 仅作趋势性参考，非论文等价判定标准。",
        "CPU 隔离容器执行，运行时长与论文硬件环境不可直接对比。",
        "确定性代码模板仅覆盖 LeNet-5 + MNIST；其他方法/数据集不在 v1 验证范围。",
        "本报告任何状态都不构成完整复现（Full Reproduction）结论。",
    ]


DELEGATED_DISCLAIMER = "本实验关键假设由系统自动接受，未经人工逐项核验；结果仅供趋势参考。"


def _has_source(value, source: ConfirmationSource | None = None) -> bool:
    if isinstance(value, dict):
        confirmed = value.get("confirmed_by")
        if confirmed is not None and (source is None or confirmed == source):
            return True
        return any(_has_source(item, source) for item in value.values())
    if isinstance(value, list):
        return any(_has_source(item, source) for item in value)
    return False


def _review_context(request: EvaluateRequest) -> dict:
    plan = request.plan
    decisions = list({decision.id: decision for decision in request.review_decisions}.values())
    payload = [
        plan.model_dump(mode="json"), request.card.model_dump(mode="json"),
        [decision.model_dump(mode="json") for decision in decisions],
    ]
    # A default supplied while parsing an old plan is not evidence of review.
    has_review_evidence = bool(decisions) or _has_source(payload) or plan.delegated_at is not None
    mode = plan.review_mode if has_review_evidence or (
        "review_mode" in plan.model_fields_set
    ) else None
    automatic = _has_source(payload, ConfirmationSource.AUTO)
    if mode is ReviewMode.DELEGATED or automatic:
        disclaimer = DELEGATED_DISCLAIMER
    elif not has_review_evidence:
        disclaimer = "审核来源未知（unknown）：缺少持久审核事件，不能认定已人工逐项核验。"
    elif any(a.requires_confirmation and not a.human_confirmed for a in plan.assumptions):
        disclaimer = (
            "仍有未经人工确认的假设（含常规项），不能认定已人工逐项核验；"
            "结果仅供趋势参考。"
        )
    else:
        disclaimer = "审核来源见逐项记录；人工或 AI 辅助确认不等于论文原文证据。"
    return dict(
        review_mode=mode,
        delegated_at=plan.delegated_at,
        delegated_by=plan.delegated_by,
        review_decisions=decisions,
        review_disclaimer=disclaimer,
    )


def evaluate_run(request: EvaluateRequest) -> EvaluateResponse:
    facts = extract_facts(request.code_files)
    method_checks = validate_method(request.card, facts)
    experiment_checks = validate_experiment(
        request.plan, facts, request.metrics, request.audit, request.execution_time
    )
    comparisons = compare_results(request.card, request.plan, request.metrics, request.run_status)
    requested = request.plan.evaluation_config.target_metric
    target = execution_metric(requested) or requested
    status = decide_final_status(
        request.run_status, method_checks, experiment_checks, comparisons, target
    )
    summary = build_summary(status, comparisons, method_checks, experiment_checks, target)
    parts = {
        "method_checks": method_checks,
        "experiment_checks": experiment_checks,
        "metric_comparisons": comparisons,
    }
    review = _review_context(request)
    references = request.references.model_copy(update={
        "review_trace_ids": [decision.id for decision in review["review_decisions"]],
    })
    report = EvaluationReport(
        references=references,
        target_metric=target,
        parameter_comparisons=parameter_comparisons(request, method_checks, experiment_checks),
        final_status=status,
        summary=summary,
        scaling_deviations=request.plan.deviations_from_paper,
        missing_information=_collect_missing(request, parts),
        assumptions=request.plan.assumptions,
        limitations=_limitations(request),
        **review,
        **parts,
    )
    return EvaluateResponse(report=report, markdown=render_markdown(request, report, facts))
