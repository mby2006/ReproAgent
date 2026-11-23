"""Render the comparison report. Numbers come only from verified pair runs."""

from reproagent_schemas import (
    CheckStatus,
    ComparisonOutcome,
    ComparisonPlan,
    ComparisonReport,
    ComparisonReportRequest,
    ComparisonReportResponse,
    ComparisonStatus,
    EvidenceRef,
    GroupKey,
    ParamSource,
    PendingStatus,
    RunObservation,
)

_SOURCE_LABEL = {
    ParamSource.PAPER_EXPLICIT: "原文明确",
    ParamSource.INFERRED: "推断/待确认",
    ParamSource.HUMAN_CONFIRMED: "人工确认",
    ParamSource.MVP_SCALING: "缩放配置",
}
_STATUS_LABEL = {
    ComparisonStatus.COMPLETED: "对照完成",
    ComparisonStatus.INCOMPLETE: "对照未完成",
    ComparisonStatus.FAILED: "执行失败",
    ComparisonStatus.RUNNING: "执行中",
    ComparisonStatus.QUEUED: "排队中",
}
_CHECK_LABEL = {
    CheckStatus.MATCHED: "一致",
    CheckStatus.DEVIATED: "不一致",
    CheckStatus.UNKNOWN: "未核实",
    CheckStatus.NOT_APPLICABLE: "不适用",
}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _counts(obs: RunObservation | None) -> str:
    if obs is None or obs.correct_count is None or not obs.evaluated_count:
        return "—"
    return f"{obs.correct_count}/{obs.evaluated_count}"


def _group_rows(plan: ComparisonPlan) -> list[str]:
    rows = ["| 组 | 优化器 | 学习率 | 结果 | 来源 |", "|---|---|---|---|---|"]
    for group in plan.groups:
        result = group.reported_result or next(
            (ref for ref in group.references if ref.value is not None), None
        )
        reported = (
            f"{result.metric} {result.value}"
            if result and result.metric and result.value is not None
            else "原文未给出"
        )
        rows.append(
            f"| {group.label} | {group.optimizer.value.upper()} | {group.learning_rate.value:g} "
            f"| {reported} | {_SOURCE_LABEL[group.learning_rate.source]} |"
        )
    return rows


def _common_rows(plan: ComparisonPlan) -> list[str]:
    base = plan.base_plan
    rows = [
        "| 共同条件 | 取值 | 来源 |",
        "|---|---|---|",
        f"| 数据集 | MNIST 训练 {base.dataset_config.train_subset_size} / "
        f"测试 {base.dataset_config.test_subset_size} | 缩放配置 |",
        f"| 模型 | {base.model_cfg.architecture} | {_SOURCE_LABEL[base.model_cfg.source]} |",
        f"| 损失函数 | {base.training_config.loss_function.value} | "
        f"{_SOURCE_LABEL[base.training_config.loss_function.source]} |",
        f"| 批大小 | {base.training_config.batch_size.value} | 缩放配置 |",
        f"| 训练轮数 | {base.training_config.epochs.value} | 缩放配置 |",
        f"| 主指标 | {plan.question.target_metric} | 原文/Card |",
        f"| 种子 | {', '.join(str(seed) for seed in plan.seeds)} | 计划 |",
        f"| 单次预算 | {base.runtime_budget.max_seconds}s | 计划 |",
    ]
    return rows


def _result_rows(outcome: ComparisonOutcome) -> list[str]:
    rows = [
        "| 种子 | A 正确/测试 | A 重算准确率 | B 正确/测试 | B 重算准确率 "
        "| 差值（百分点） | 配对核验 |",
        "|---|---|---|---|---|---|---|",
    ]
    for seed in outcome.seeds:
        delta = (
            "—"
            if seed.delta_percentage_points is None
            else f"{seed.delta_percentage_points:+.2f}"
        )
        verified = "通过" if seed.verification and seed.verification.verified else "未通过"
        rows.append(
            f"| {seed.seed} | {_counts(seed.group_a)} "
            f"| {_pct(seed.recomputed_accuracy_a)} "
            f"| {_counts(seed.group_b)} "
            f"| {_pct(seed.recomputed_accuracy_b)} | {delta} | {verified} |"
        )
    if len(outcome.seeds) > 1:
        mean_delta = (
            "—" if outcome.mean_delta_percentage_points is None
            else f"{outcome.mean_delta_percentage_points:+.2f}"
        )
        rows.append(
            f"| **均值** | — | {_pct(outcome.mean_a)} | — | {_pct(outcome.mean_b)} "
            f"| {mean_delta} | {outcome.completed_pairs}/{len(outcome.seeds)} 对通过 |"
        )
    return rows


def _verification_rows(outcome: ComparisonOutcome) -> list[str]:
    rows = ["| 种子 | 核验项 | 状态 | 说明 |", "|---|---|---|---|"]
    for verification in outcome.verifications:
        for check in verification.checks:
            rows.append(
                f"| {verification.seed} | {check.item.value} | {_CHECK_LABEL[check.status]} "
                f"| {check.note} |"
            )
    return rows


def _pending_rows(request: ComparisonReportRequest) -> list[str]:
    rows = ["| 项目 | 处理 | 取值 | 依据 |", "|---|---|---|---|"]
    for item in request.plan.pending:
        if item.status is PendingStatus.PENDING:
            decision = "未处理"
        elif item.status is PendingStatus.ADOPTED:
            decision = "采用建议"
        else:
            decision = "用户补充"
        value = item.resolved_value if item.resolved else item.suggested_value
        rows.append(
            f"| {item.label}（{item.field_path}） | {decision} | {value} "
            f"| {item.resolved_source or item.suggestion_source or '—'} |"
        )
    return rows


def _summary(request: ComparisonReportRequest, final_status: ComparisonStatus) -> str:
    plan, outcome = request.plan, request.outcome
    left, right = plan.group(GroupKey.A), plan.group(GroupKey.B)
    if (
        final_status is ComparisonStatus.INCOMPLETE
        and outcome.status is ComparisonStatus.COMPLETED
    ):
        # The runs finished, but the comparison was withdrawn (question replaced):
        # the numbers stay on record without becoming a conclusion.
        return (
            f"本次配对运行本身已完成 {outcome.completed_pairs} 对可核验实验，"
            "但该对照因问题被替换而作废；下列差值只作为过程记录，不作为结论使用。"
        )
    if outcome.status is ComparisonStatus.COMPLETED and outcome.mean_delta is not None:
        winner = left.label if outcome.direction == "a_higher" else (
            right.label if outcome.direction == "b_higher" else "两组"
        )
        return (
            f"在相同数据划分、初始权重与样本顺序下运行 {outcome.completed_pairs} 对实验，"
            f"{winner}的 {plan.question.target_metric} "
            f"{'高出' if outcome.direction != 'equal' else '持平'} "
            f"{abs(outcome.mean_delta_percentage_points or 0):.2f} 个百分点"
            f"（{left.label} {_pct(outcome.mean_a)}，{right.label} {_pct(outcome.mean_b)}）。"
            "这是缩放实验的实测差值，不构成论文完整复现。"
        )
    if outcome.status is ComparisonStatus.INCOMPLETE:
        return (
            f"已完成 {outcome.completed_pairs}/{outcome.requested_pairs} 对可核验实验，"
            "其余配对因失败或共同条件核验未通过而阻塞，因此不给出对照结论。"
        )
    return "本次对照没有可核验的配对结果，暂不给出任何差值结论。"


def build_comparison_report(request: ComparisonReportRequest) -> ComparisonReportResponse:
    plan = request.plan
    outcome = request.outcome
    title = request.paper_title or "论文对照实验"
    # An explicit note means the comparison was withdrawn (e.g. the question was
    # replaced mid-run): the runs are still recorded, but the status stays open.
    final_status = (
        ComparisonStatus.INCOMPLETE if request.review_notes else outcome.status
    )
    summary = _summary(request, final_status)

    parameter_sources = [
        EvidenceRef(
            kind="paper",
            quote=group.references[0].quote if group.references else None,
            source_page=group.references[0].source_page if group.references else None,
            detail=f"{group.label} 优化器 {group.optimizer.value.upper()}（"
                   f"{_SOURCE_LABEL[group.optimizer.source]}）",
        )
        for group in plan.groups
    ]

    lines: list[str] = [
        f"# 优化器对照报告：{title}",
        "",
        f"**状态：{_STATUS_LABEL.get(final_status, final_status.value)}**",
        "",
        *[f"> {note}" for note in request.review_notes],
        *([""] if request.review_notes else []),
        summary,
        "",
        "## 1. 实验问题",
        "",
        f"- 用户问题：{plan.question.question}",
        f"- 规范化问题：{plan.question.restatement}",
        f"- 主指标：{plan.question.target_metric}",
        f"- 重复依据：{plan.question.mean_basis or '单次对照'}",
        "",
        "## 2. 论文依据",
        "",
    ]
    for group in plan.groups:
        lines.append(f"**{group.label}**")
        for reference in group.references:
            quote = f"「{reference.quote}」" if reference.quote else "（无直接引文）"
            lines.append(
                f"- {reference.label} · p.{reference.source_page or '?'}"
                f"{' · 表 ' + reference.table_id if reference.table_id else ''}：{quote}"
            )
        lines.append("")
    if not any(group.references for group in plan.groups):
        lines.append("原文未定位到可核对的出处，已在待确认项中标记。")
        lines.append("")

    lines += [
        "## 3. 共同条件",
        "",
        *_common_rows(plan),
        "",
        "## 4. 两组配置（仅差异项由组决定）",
        "",
        *_group_rows(plan),
        "",
        "## 5. 待确认项的处理",
        "",
        *_pending_rows(request),
        "",
        "## 6. 缩放与授权",
        "",
        f"- {plan.authorization.note}",
        f"- 训练子集 {plan.base_plan.dataset_config.train_subset_size}，"
        f"测试子集 {plan.base_plan.dataset_config.test_subset_size}，"
        f"{plan.base_plan.training_config.epochs.value} epoch。",
        "- 缩放实验改变的是训练预算，不是论文设置；差值只在本次条件下成立。",
        "",
        "## 7. 实测结果（准确率由正确数/测试数重算）",
        "",
        *_result_rows(outcome),
        "",
    ]
    if outcome.direction:
        direction_label = {
            "a_higher": "A 组更高",
            "b_higher": "B 组更高",
            "equal": "两组相同",
        }[outcome.direction]
        lines.append(f"- 方向：{direction_label}（差值为 A 组减 B 组）")
    lines += [
        "- 重算方式：accuracy = correct_count ÷ evaluated_count，未采用训练脚本自报指标。",
        "",
        "## 8. 配对核验",
        "",
        *_verification_rows(outcome),
        "",
        "## 9. 关键假设",
        "",
        *[f"- {item}" for item in plan.assumptions],
        "",
        "## 10. 局限与说明",
        "",
    ]
    lines += [f"- {item}" for item in plan.limitations]
    for blocker in outcome.blockers:
        lines.append(f"- 阻塞：{blocker}")
    lines += [
        "",
        "## 11. 产物与引用",
        "",
        f"- 对照计划版本：v{request.references.plan_version}"
        f"（`{request.references.plan_version_id}`）",
        f"- Card 版本：v{request.references.spec_version}"
        f"（`{request.references.spec_version_id}`）",
        "- 运行记录：" + "、".join(f"`{run_id}`" for run_id in request.references.run_ids),
        f"- 镜像摘要：{request.references.image_digest or '未核实'}",
        f"- 代码哈希：{', '.join(request.references.code_hashes) or '未核实'}",
        "",
    ]

    markdown = "\n".join(lines)
    report = ComparisonReport(
        references=request.references,
        question=plan.question,
        groups=plan.groups,
        base_plan=plan.base_plan,
        seeds=plan.seeds,
        outcome=outcome,
        pending_decisions=plan.pending,
        pair_verifications=outcome.verifications,
        paper_references=plan.paper_references,
        assumptions=plan.assumptions,
        limitations=plan.limitations,
        final_status=final_status,
        summary=summary,
        parameter_sources=parameter_sources,
        scaling_note=(
            f"缩放实验：训练 {plan.base_plan.dataset_config.train_subset_size} 张、"
            f"测试 {plan.base_plan.dataset_config.test_subset_size} 张、"
            f"{plan.base_plan.training_config.epochs.value} epoch；"
            "报告只描述本条件下的差值，不声称复现论文全部结果。"
        ),
    )
    return ComparisonReportResponse(report=report, markdown=markdown)
