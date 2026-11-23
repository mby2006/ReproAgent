"""Deterministic final-status decision tree.

Order matters; the first matching rule wins:
1. EXECUTION_FAILED      -- run did not succeed.
2. DEVIATION_FOUND       -- any method/experiment check deviated (implementation
                            or execution contradicts paper evidence / approved plan).
3. INSUFFICIENT_EVIDENCE -- target metric lacks paper or experiment value, or a
                            critical method item (architecture/loss/dataset) is unknown.
4. PARTIAL_MATCH         -- some metric comparisons matched and others deviated.
5. METHOD_VALIDATED      -- critical method items matched, no deviations anywhere,
                            target metric within tolerance. STILL a scaled run:
                            this is never a full-reproduction claim.
6. SCALED_RUN_COMPLETED  -- faithful scaled run completed, but the target metric
                            is outside tolerance (expected under MVP scaling).
"""

from reproagent_schemas import (
    CheckStatus,
    ExperimentCheck,
    FinalStatus,
    MethodCheck,
    MethodItem,
    MetricComparison,
    RunStatus,
)

_CRITICAL_METHOD_ITEMS = {MethodItem.ARCHITECTURE, MethodItem.LOSS_FUNCTION, MethodItem.DATASET}


def decide_final_status(
    run_status: RunStatus,
    method_checks: list[MethodCheck],
    experiment_checks: list[ExperimentCheck],
    comparisons: list[MetricComparison],
    target_metric: str = "accuracy",
) -> FinalStatus:
    if run_status is not RunStatus.SUCCEEDED:
        return FinalStatus.EXECUTION_FAILED

    checks = [*method_checks, *experiment_checks]
    if any(c.status is CheckStatus.DEVIATED for c in checks):
        return FinalStatus.DEVIATION_FOUND

    target = next((c for c in comparisons if c.metric == target_metric), None)
    if target is None or target.paper_value is None or target.experiment_value is None:
        return FinalStatus.INSUFFICIENT_EVIDENCE

    critical = [c for c in method_checks if c.item in _CRITICAL_METHOD_ITEMS]
    if not critical or any(c.status is not CheckStatus.MATCHED for c in critical):
        return FinalStatus.INSUFFICIENT_EVIDENCE

    matched = [c for c in comparisons if c.comparison_status is CheckStatus.MATCHED]
    deviated = [c for c in comparisons if c.comparison_status is CheckStatus.DEVIATED]
    if matched and deviated:
        return FinalStatus.PARTIAL_MATCH
    if target.comparison_status is CheckStatus.MATCHED and not deviated:
        return FinalStatus.METHOD_VALIDATED
    return FinalStatus.SCALED_RUN_COMPLETED


def build_summary(
    status: FinalStatus,
    comparisons: list[MetricComparison],
    method_checks: list[MethodCheck],
    experiment_checks: list[ExperimentCheck],
    target_metric: str = "accuracy",
) -> str:
    target = next((c for c in comparisons if c.metric == target_metric), None)
    numbers = ""
    if (
        target is not None
        and target.paper_value is not None
        and target.experiment_value is not None
    ):
        numbers = (
            f"{target.metric}（{target.unit}）：论文 {target.paper_value:.4f} vs 实验 {target.experiment_value:.4f}"
            f"（绝对差 {target.absolute_difference:.4f}"
            + (
                f"，{target.percentage_point_difference:+.2f} 个百分点"
                if target.percentage_point_difference is not None
                else ""
            )
            + "）。"
        )
    n_method_dev = sum(c.status is CheckStatus.DEVIATED for c in method_checks)
    n_exp_dev = sum(c.status is CheckStatus.DEVIATED for c in experiment_checks)
    descriptions = {
        FinalStatus.EXECUTION_FAILED: "执行未成功，无法进行结果对比；报告记录失败原因与已有审计。",
        FinalStatus.DEVIATION_FOUND: (
            f"发现偏差：方法验证 {n_method_dev} 项、实验验证 {n_exp_dev} 项与证据/计划不一致。"
        ),
        FinalStatus.INSUFFICIENT_EVIDENCE: "论文证据或运行数据不足，无法做出匹配判定。",
        FinalStatus.PARTIAL_MATCH: "部分指标在容差内匹配、部分超出；存在混合结果。",
        FinalStatus.METHOD_VALIDATED: (
            "方法实现与论文证据一致，实验按计划执行，目标指标在趋势容差内。"
        ),
        FinalStatus.SCALED_RUN_COMPLETED: (
            "方法实现忠实、缩放实验已完整执行，但目标指标超出趋势容差；"
            "缩放配置下这不构成复现失败定论。"
        ),
    }
    return (
        f"{descriptions[status]}{numbers}"
        "本运行是缩小规模的 MVP 实验，任何状态都不构成对论文完整复现（Full Reproduction）的判定。"
    )
