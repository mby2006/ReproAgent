"""Build an approvable comparison plan from a grounded Card + user's question.

Shared conditions come from the existing single-group planner, so a pair inherits
exactly the same dataset/model/loss/batch/epochs/budget provenance as a normal
reproduction. Only the factor under test (optimizer + its learning rate) is
group-specific. A group value the paper never states is never invented: it lands
in ``pending`` with a suggestion, its source, and the ranking risk.
"""

import re

from reproagent_schemas import (
    ComparisonGroup,
    ComparisonGroupSpec,
    ComparisonPlan,
    ComparisonPlanRequest,
    ComparisonPlanResponse,
    ComparisonQuestion,
    EvidenceStatus,
    GroupKey,
    PaperReference,
    ParamSource,
    PendingParam,
    PlanParam,
    ReproductionCard,
    RiskTier,
    ScalingAuthorization,
)

from ..planning.assumptions import as_float, normalize_optimizer
from ..planning.engine import UnsupportedPaperError, build_plan
from ..planning.scaling import MVP_SEED, ScalingConfig
from .support import MAX_EPOCHS, MAX_SECONDS, MAX_TEST_SAMPLES, MAX_TRAIN_SAMPLES, support_check

_MEAN_HINTS = re.compile(r"均值|平均|多次|重复|方差|波动|稳定|mean|average|variance|std", re.I)

# Suggestions are conservative, textbook defaults -- never presented as the
# paper's own value, and always paired with an explicit impact statement.
_SUGGESTED_LR = {
    "sgd": 0.01,
    "adam": 0.001,
    "adamw": 0.001,
    "adagrad": 0.01,
    "adadelta": 1.0,
    "rmsprop": 0.001,
}


class UnsupportedComparisonError(ValueError):
    """The selected question cannot be answered by the current templates."""

    def __init__(self, message: str, gaps: list[str] | None = None):
        super().__init__(message)
        self.gaps = gaps or []


def _metric_of(card: ReproductionCard) -> str:
    if card.reported_results:
        return str(card.reported_results[0].metric)
    if card.evaluation_metrics and card.evaluation_metrics[0].value is not None:
        return str(card.evaluation_metrics[0].value)
    return "accuracy"


def _seeds(request: ComparisonPlanRequest) -> list[int]:
    """A mean question needs at least three pairs; never claim three and run one.

    Callers may pin seeds (a rerun must reuse them), but a question that asks
    for a mean cannot be answered by a single pair, so the list is completed
    with deterministic follow-up seeds instead of silently keeping one.
    """
    requires_mean = bool(_MEAN_HINTS.search(request.question))
    if request.seeds:
        seeds = list(dict.fromkeys(request.seeds))
    elif requires_mean:
        seeds = [MVP_SEED, MVP_SEED + 1, MVP_SEED + 2]
    else:
        seeds = [MVP_SEED]
    if requires_mean:
        while len(seeds) < 3:
            seeds.append(max(seeds) + 1)
    return seeds


def _question(request: ComparisonPlanRequest, seeds: list[int]) -> ComparisonQuestion:
    card = request.card
    groups = {spec.key: spec for spec in request.group_specs}
    left, right = groups[GroupKey.A], groups[GroupKey.B]
    metric = _metric_of(card)
    repeated = len(seeds) > 1
    restatement = (
        f"在完全相同的训练与测试条件下，{left.label}（{left.optimizer.upper()}）"
        f"与 {right.label}（{right.optimizer.upper()}）的 {metric} 是否存在可核验的差异？"
    )
    if repeated:
        mean_basis = (
            f"问题要求核查多次运行的均值，按证据用 {len(seeds)} 个相同种子"
            f"（{', '.join(str(seed) for seed in seeds)}）逐对运行"
        )
    else:
        mean_basis = "问题只要求单次对照，默认单个种子运行一对"
    return ComparisonQuestion(
        question=request.question,
        restatement=restatement,
        factor="optimizer",
        target_metric=metric,
        requires_mean=repeated,
        mean_basis=mean_basis,
    )


def _reference_for(spec: ComparisonGroupSpec, card: ReproductionCard) -> list[PaperReference]:
    if spec.references:
        return spec.references
    optimizer = normalize_optimizer(spec.optimizer)
    card_optimizer = normalize_optimizer(str(card.optimizer.value or ""))
    if optimizer is not None and optimizer == card_optimizer:
        label = "Card 优化器证据"
        return [
            PaperReference(
                label=label,
                quote=card.optimizer.evidence_quote,
                source_page=card.optimizer.source_page,
                source_section=card.optimizer.source_section,
                metric=_metric_of(card),
            )
        ]
    return []


def _optimizer_param(
    spec: ComparisonGroupSpec, card: ReproductionCard, pending: list[PendingParam],
) -> PlanParam[str]:
    normalized = normalize_optimizer(spec.optimizer)
    if normalized is None:
        raise UnsupportedComparisonError(f"不支持的优化器：{spec.optimizer}")
    field_path = f"groups.{spec.key.value}.optimizer"
    card_optimizer = normalize_optimizer(str(card.optimizer.value or ""))
    if normalized == card_optimizer and card.optimizer.status in (
        EvidenceStatus.PAPER_EXPLICIT,
        EvidenceStatus.PAPER_DESCRIBED,
    ):
        return PlanParam(
            value=normalized,
            source=ParamSource.PAPER_EXPLICIT,
            paper_value=str(card.optimizer.value),
            evidence_quote=card.optimizer.evidence_quote,
            source_page=card.optimizer.source_page,
            confidence=card.optimizer.confidence,
        )
    quote = next((ref.quote for ref in spec.references if ref.quote), None)
    page = next((ref.source_page for ref in spec.references if ref.source_page), None)
    if quote and page:
        return PlanParam(
            value=normalized,
            source=ParamSource.PAPER_EXPLICIT,
            paper_value=spec.optimizer,
            evidence_quote=quote,
            source_page=page,
        )
    # A reference the user supplied (label + its own source, no page/quote) is a
    # human decision; re-planning for a new question must not reopen it.
    user_backed = next(
        (
            ref
            for ref in spec.references
            if ref.source_section and not ref.quote and ref.source_page is None
        ),
        None,
    )
    if user_backed is not None:
        return PlanParam(
            value=normalized,
            source=ParamSource.HUMAN_CONFIRMED,
            paper_value=None,
            rationale=f"用户补充依据：{user_backed.source_section}（{user_backed.label}）",
            human_confirmed=True,
            confirmed_by="human",
        )
    rationale = "该组优化器来自用户在对照计划中的选择，原文未提供可直接引用的设置"
    pending.append(
        PendingParam(
            id=f"{field_path}.source",
            group_key=spec.key,
            field_path=field_path,
            label=f"{spec.label} 优化器依据",
            reason="原文未定位到该组优化器的直接证据",
            impact="缺少原文依据时，该组是否代表论文设置无法核对，可能改变对照结论的含义",
            suggested_value=normalized,
            suggestion_source="用户在对照计划中的选择",
            risk_tier=RiskTier.CRITICAL,
        )
    )
    return PlanParam(
        value=normalized,
        source=ParamSource.INFERRED,
        paper_value=None,
        rationale=rationale,
        requires_confirmation=True,
    )


def _lr_reference(spec: ComparisonGroupSpec) -> PaperReference | None:
    """Only a reference that actually talks about the learning rate can back it."""
    for reference in spec.references:
        haystack = f"{reference.metric or ''} {reference.quote or ''}".lower()
        if "learning rate" in haystack or "lr" in haystack or "学习率" in haystack:
            return reference
    return None


def _learning_rate_param(
    spec: ComparisonGroupSpec,
    card: ReproductionCard,
    pending: list[PendingParam],
) -> PlanParam[float]:
    field_path = f"groups.{spec.key.value}.learning_rate"
    card_lr_field = card.hyperparameters.get("learning_rate")
    card_lr = as_float(card_lr_field.value) if card_lr_field is not None else None
    card_backed = (
        card_lr is not None
        and card_lr_field is not None
        and card_lr_field.status
        in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
    )

    if spec.learning_rate is not None:
        if card_backed and abs(card_lr - spec.learning_rate) < 1e-12:
            return PlanParam(
                value=spec.learning_rate,
                source=ParamSource.PAPER_EXPLICIT,
                paper_value=card_lr,
                evidence_quote=card_lr_field.evidence_quote,
                source_page=card_lr_field.source_page,
            )
        reference = _lr_reference(spec)
        if reference is not None:
            return PlanParam(
                value=spec.learning_rate,
                source=ParamSource.PAPER_EXPLICIT,
                paper_value=spec.learning_rate,
                evidence_quote=reference.quote,
                source_page=reference.source_page,
            )
        # The user typed a value for this group: that is a human decision, not a
        # system guess, so it does not block approval.
        return PlanParam(
            value=spec.learning_rate,
            source=ParamSource.HUMAN_CONFIRMED,
            paper_value=None,
            rationale="用户在对照计划中为该组指定学习率（未附学习率原文依据）",
            human_confirmed=True,
            confirmed_by="human",
        )

    if card_backed:
        return PlanParam(
            value=card_lr,
            source=ParamSource.PAPER_EXPLICIT,
            paper_value=card_lr,
            evidence_quote=card_lr_field.evidence_quote,
            source_page=card_lr_field.source_page,
        )

    suggestion = _SUGGESTED_LR.get(spec.optimizer, 0.01)
    pending.append(
        PendingParam(
            id=f"{field_path}.value",
            group_key=spec.key,
            field_path=field_path,
            label=f"{spec.label} 学习率",
            reason="原文未给出该组学习率",
            impact="学习率直接决定收敛速度与最终准确率；缺省取值可能改变两组的排名",
            suggested_value=suggestion,
            suggestion_source=f"{spec.optimizer.upper()} 常用初始值（非原文设置）",
            risk_tier=RiskTier.CRITICAL,
        )
    )
    return PlanParam(
        value=suggestion,
        source=ParamSource.INFERRED,
        paper_value=None,
        rationale="原文未给出学习率，暂用常用初始值，需人工确认或补充来源",
        requires_confirmation=True,
    )


def _weight_decay_param(
    spec: ComparisonGroupSpec, card: ReproductionCard
) -> PlanParam[float] | None:
    if spec.weight_decay is not None:
        return PlanParam(
            value=spec.weight_decay,
            source=ParamSource.INFERRED,
            paper_value=spec.weight_decay,
            rationale="用户在对照计划中指定",
        )
    field = card.hyperparameters.get("weight_decay")
    if field is not None and field.value is not None and field.status in (
        EvidenceStatus.PAPER_EXPLICIT,
        EvidenceStatus.PAPER_DESCRIBED,
    ):
        value = as_float(field.value)
        if value is not None:
            return PlanParam(
                value=value,
                source=ParamSource.PAPER_EXPLICIT,
                paper_value=value,
                evidence_quote=field.evidence_quote,
                source_page=field.source_page,
            )
    return None


def _scaling_pending(
    request: ComparisonPlanRequest,
    base_train: int,
    base_test: int,
    base_epochs: int,
    base_seconds: int,
) -> list[PendingParam]:
    pending: list[PendingParam] = []
    checks = (
        ("train_subset_size", request.train_subset_size, base_train, MAX_TRAIN_SAMPLES),
        ("test_subset_size", request.test_subset_size, base_test, MAX_TEST_SAMPLES),
        ("epochs", request.epochs, base_epochs, MAX_EPOCHS),
        ("max_seconds", request.max_seconds, base_seconds, MAX_SECONDS),
    )
    for name, requested, _actual, ceiling in checks:
        if requested is None:
            continue
        if requested > ceiling:
            pending.append(
                PendingParam(
                    id=f"scaling.{name}",
                    field_path=name,
                    label=f"缩放参数 {name}",
                    reason=f"请求值 {requested} 超出授权上限 {ceiling}",
                    impact="扩大训练预算会改变原问题的可比条件，需先确认后才会执行",
                    suggested_value=ceiling,
                    suggestion_source="当前模板的授权上限",
                    risk_tier=RiskTier.CRITICAL,
                )
            )
    return pending


def build_comparison_plan(request: ComparisonPlanRequest) -> ComparisonPlanResponse:
    # Support is checked against the user's request *before* any planning, so
    # the upload page can name the exact missing capability instead of a generic
    # "unsupported paper".
    support = support_check(
        card=request.card,
        groups=request.group_specs,
        target_metric=_metric_of(request.card),
        max_seconds=request.max_seconds,
    )
    if not support.ok:
        raise UnsupportedComparisonError(
            "该论文不满足对照条件：" + "；".join(gap.detail for gap in support.missing),
            gaps=[
                f"{gap.field}: {gap.detail}（要求：{gap.expectation}）"
                for gap in support.missing
            ],
        )

    try:
        base_plan = build_plan(
            request.card,
            ScalingConfig(
                train_subset_size=request.train_subset_size
                or ScalingConfig().train_subset_size,
                test_subset_size=request.test_subset_size or ScalingConfig().test_subset_size,
                epochs=request.epochs or ScalingConfig().epochs,
                batch_size=request.batch_size or ScalingConfig().batch_size,
                max_seconds=request.max_seconds or ScalingConfig().max_seconds,
            ),
        )
    except UnsupportedPaperError as exc:
        raise UnsupportedComparisonError(
            str(exc), gaps=[f"dataset/model: {exc}"]
        ) from exc

    seeds = _seeds(request)
    question = _question(request, seeds)
    seed_note = (
        f"问题需要多次运行求均值，已将种子补齐为 {len(seeds)} 个"
        f"（{', '.join(str(seed) for seed in seeds)}）逐对运行"
        if len(seeds) > 1 and len(seeds) > len(request.seeds or [])
        else None
    )
    pending: list[PendingParam] = _scaling_pending(
        request,
        base_plan.dataset_config.train_subset_size,
        base_plan.dataset_config.test_subset_size,
        base_plan.training_config.epochs.value,
        base_plan.runtime_budget.max_seconds,
    )

    groups: list[ComparisonGroup] = []
    for spec in request.group_specs:
        references = _reference_for(spec, request.card)
        if not references:
            pending.append(
                PendingParam(
                    id=f"groups.{spec.key.value}.references",
                    group_key=spec.key,
                    field_path=f"groups.{spec.key.value}.references",
                    label=f"{spec.label} 原文出处",
                    reason="未在该组找到可核对的页码或表格位置",
                    impact="无法确认结果属于该组，配对核验会被阻断",
                    suggested_value=None,
                    suggestion_source=None,
                    risk_tier=RiskTier.CRITICAL,
                )
            )
        groups.append(
            ComparisonGroup(
                key=spec.key,
                label=spec.label,
                optimizer=_optimizer_param(spec, request.card, pending),
                learning_rate=_learning_rate_param(spec, request.card, pending),
                weight_decay=_weight_decay_param(spec, request.card),
                references=references,
                reported_result=spec.reported_result,
            )
        )

    authorization = ScalingAuthorization(
        max_train_samples=MAX_TRAIN_SAMPLES,
        max_test_samples=MAX_TEST_SAMPLES,
        max_epochs=MAX_EPOCHS,
        max_seconds=MAX_SECONDS,
        max_seeds=3,
        note="授权范围：MNIST 子集训练、不超过 20 epoch、单次运行不超过 600 秒",
    )

    paper_refs: list[PaperReference] = []
    for group in groups:
        paper_refs.extend(group.references)
    for reported in request.card.reported_results:
        paper_refs.append(
            PaperReference(
                label=(
                    f"原文报告 · p.{reported.source_page}"
                    if reported.source_page
                    else "原文报告"
                ),
                quote=reported.evidence_quote,
                source_page=reported.source_page,
                source_section=reported.source_section,
                metric=str(reported.metric),
                value=reported.value,
                condition=reported.condition,
            )
        )

    plan = ComparisonPlan(
        question=question,
        base_plan=base_plan,
        groups=groups,
        seeds=seeds,
        authorization=authorization,
        support=support,
        pending=pending,
        paper_references=paper_refs,
        assumptions=[
            "两组共用同一数据划分、初始权重、样本顺序、训练预算与评估条件；"
            "差异只来自优化器及其学习率。",
            "准确率由正确样本数除以测试样本数独立重算，不采用训练脚本自报的指标。",
            *([seed_note] if seed_note else []),
        ],
        limitations=[
            "缩放实验（子集训练/少量 epoch）不构成论文的完整复现。",
            "单次运行的差值含随机波动；若问题要求均值，需按种子列表逐对重复。",
            "论文未给出的参数已列在待确认项中，确认前不会执行。",
        ],
        notes=(
            "本次对照只回答「哪一组在本环境下更高」，不外推为论文结论的普遍成立。"
        ),
    )
    return ComparisonPlanResponse(plan=plan, support=support)
