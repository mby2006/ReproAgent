"""Deterministic Experiment Planner: Reproduction Card -> executable MVP plan.

No LLM is involved. Every parameter traces to paper evidence, a flagged
inference/proposal, or the explicitly-tagged MVP scaling profile. v1 supports
only LeNet-5 + MNIST; anything else is rejected loudly.
"""

import math
from typing import TYPE_CHECKING

from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    DatasetConfig,
    Deviation,
    EvaluationConfig,
    EvidenceField,
    EvidenceStatus,
    ExpectedRuntime,
    ExperimentPlan,
    ModelConfig,
    PaperSupportCheck,
    ParamSource,
    PlanParam,
    PreprocessingConfig,
    PreprocessingStep,
    ReproductionCard,
    ResourceEstimates,
    RiskTier,
    RuntimeBudget,
    TrainingConfig,
)

if TYPE_CHECKING:
    from reproagent_schemas import ParsedDocument

from .assumptions import (
    as_float,
    as_int,
    confirmation_source,
    inferred_source,
    normalize_loss,
    normalize_optimizer,
    param_from_evidence,
)
from .scaling import (
    MNIST_FULL_TEST,
    MNIST_FULL_TRAIN,
    MNIST_INPUT_SHAPE,
    MNIST_NORMALIZE_MEAN,
    MNIST_NORMALIZE_STD,
    MNIST_NUM_CLASSES,
    MVP_DEVICE,
    MVP_SEED,
    MVP_TOLERANCE,
    SCALING_PROFILE,
    ScalingConfig,
)

# The support-gate model is shared with the API client via reproagent_schemas.
# ``SupportCheck`` is kept as a local alias so in-repo callers/tests are stable.
SupportCheck = PaperSupportCheck


class UnsupportedPaperError(ValueError):
    """v1 planner only handles LeNet-5 + MNIST.

    ``code`` is a stable machine-readable reason (``UNSUPPORTED_DATASET`` /
    ``UNSUPPORTED_MODEL``) so the support gate does not have to string-match
    the message text.
    """

    code: str = "UNSUPPORTED_PAPER"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _guard_lenet_mnist(card: ReproductionCard) -> None:
    dataset = str(card.dataset.value or "").lower()
    if card.dataset.status is EvidenceStatus.MISSING or "mnist" not in dataset:
        raise UnsupportedPaperError(
            "v1 仅支持 MNIST 数据集；Card 未明确 MNIST", code="UNSUPPORTED_DATASET"
        )
    arch = str(card.model_architecture.value or "").lower()
    method = str(card.method.value or "").lower()
    if "lenet" not in arch and "lenet" not in method:
        raise UnsupportedPaperError(
            "v1 仅支持 LeNet-5 架构；Card 未明确 LeNet", code="UNSUPPORTED_MODEL"
        )


def check_supported(
    document: "ParsedDocument", card: ReproductionCard | None
) -> SupportCheck:
    """Deterministic support gate: OCR scanned PDF or unsupported model/dataset.

    The model/dataset check is delegated to ``_guard_lenet_mnist`` (the single
    source of truth for what v1 supports); the raised error's ``code`` attribute
    maps to the gate code, so no string matching on the message is needed.
    """
    if document.ocr_required_pages:
        return SupportCheck(
            supported=False, code="SCANNED_PDF", message="扫描 PDF 需要 OCR，当前版本不支持"
        )
    if card is None:
        raise ValueError("card is required after extraction")
    try:
        _guard_lenet_mnist(card)
    except UnsupportedPaperError as exc:
        if exc.code not in ("UNSUPPORTED_DATASET", "UNSUPPORTED_MODEL"):
            return SupportCheck(
                supported=False, code="UNSUPPORTED_PAPER", message=str(exc)
            )
        message = (
            "v1 仅支持 MNIST 数据集"
            if exc.code == "UNSUPPORTED_DATASET"
            else "v1 仅支持 LeNet-5 架构"
        )
        return SupportCheck(supported=False, code=exc.code, message=message)
    return SupportCheck(supported=True, code="SUPPORTED", message="LeNet-5 \u00d7 MNIST")


def _build_optimizer(card: ReproductionCard, assumptions: list[Assumption]) -> PlanParam:
    field = card.optimizer
    fp = "training_config.optimizer"
    normalized = normalize_optimizer(field.value)
    if (
        field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        and normalized is not None
    ):
        return PlanParam(
            value=normalized,
            source=ParamSource.PAPER_EXPLICIT,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            human_confirmed=field.human_confirmed,
            confirmed_by=confirmation_source(field),
            confidence=field.confidence,
        )
    if (
        field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        and normalized is None
    ):
        rationale = f"原文优化器「{field.value}」非标准实现，MVP 提出 SGD 替代，需人工确认"
        assumptions.append(
            Assumption(
                field_path=fp,
                kind=AssumptionKind.NONSTANDARD_CHOICE,
                value="sgd",
                rationale=rationale,
                requires_confirmation=True,
                risk_tier=RiskTier.CRITICAL,
                card_status=field.status,
                evidence_quote=field.evidence_quote,
                source_page=field.source_page,
            )
        )
        return PlanParam(
            value="sgd",
            source=ParamSource.INFERRED,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            rationale=rationale,
            requires_confirmation=True,
        )
    if (
        field.status is EvidenceStatus.INFERRED
        and field.value is not None
        and normalized is None
    ):
        rationale = f"推断优化器「{field.value}」非标准实现，MVP 提出 SGD 替代，需人工确认"
        assumptions.append(
            Assumption(
                field_path=fp,
                kind=AssumptionKind.NONSTANDARD_CHOICE,
                value="sgd",
                rationale=rationale,
                requires_confirmation=True,
                risk_tier=RiskTier.CRITICAL,
                card_status=EvidenceStatus.INFERRED,
                evidence_quote=field.evidence_quote,
                source_page=field.source_page,
            )
        )
        return PlanParam(
            value="sgd",
            source=ParamSource.INFERRED,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            rationale=rationale,
            requires_confirmation=True,
        )
    return param_from_evidence(
        field,
        fp,
        fallback_value="sgd",
        fallback_rationale="论文未明确优化器；LeNet-5/MNIST 常用 SGD，提出待确认",
        cast=str,
        assumptions=assumptions,
    )


def _build_loss(card: ReproductionCard, assumptions: list[Assumption]) -> PlanParam:
    field = card.loss_function
    fp = "training_config.loss_function"
    normalized = normalize_loss(field.value)
    if (
        field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        and normalized is not None
    ):
        return PlanParam(
            value=normalized,
            source=ParamSource.PAPER_EXPLICIT,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            human_confirmed=field.human_confirmed,
            confirmed_by=confirmation_source(field),
            confidence=field.confidence,
        )
    if (
        field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        and normalized is None
    ):
        rationale = f"原文损失「{field.value}」非标准实现，MVP 提出交叉熵替代，需人工确认"
        assumptions.append(
            Assumption(
                field_path=fp,
                kind=AssumptionKind.NONSTANDARD_CHOICE,
                value="cross_entropy",
                rationale=rationale,
                requires_confirmation=True,
                risk_tier=RiskTier.CRITICAL,
                card_status=field.status,
                evidence_quote=field.evidence_quote,
                source_page=field.source_page,
            )
        )
        return PlanParam(
            value="cross_entropy",
            source=ParamSource.INFERRED,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            rationale=rationale,
            requires_confirmation=True,
        )
    if (
        field.status is EvidenceStatus.INFERRED
        and field.value is not None
        and normalized is None
    ):
        rationale = f"推断损失「{field.value}」非标准实现，MVP 提出交叉熵替代，需人工确认"
        assumptions.append(
            Assumption(
                field_path=fp,
                kind=AssumptionKind.NONSTANDARD_CHOICE,
                value="cross_entropy",
                rationale=rationale,
                requires_confirmation=True,
                risk_tier=RiskTier.CRITICAL,
                card_status=EvidenceStatus.INFERRED,
                evidence_quote=field.evidence_quote,
                source_page=field.source_page,
            )
        )
        return PlanParam(
            value="cross_entropy",
            source=ParamSource.INFERRED,
            paper_value=str(field.value),
            evidence_quote=field.evidence_quote,
            source_page=field.source_page,
            rationale=rationale,
            requires_confirmation=True,
        )
    return param_from_evidence(
        field,
        fp,
        fallback_value="cross_entropy",
        fallback_rationale="论文未明确损失函数；MNIST 十分类常用交叉熵，提出待确认",
        cast=str,
        assumptions=assumptions,
    )


def _scaling_params(
    card: ReproductionCard, scaling: ScalingConfig, deviations: list[Deviation]
) -> tuple[PlanParam, PlanParam]:
    bs_field = card.hyperparameters.get("batch_size")
    bs_paper = as_int(bs_field.value) if bs_field and bs_field.value is not None else None
    batch_param = PlanParam(
        value=scaling.batch_size,
        source=ParamSource.MVP_SCALING,
        paper_value=bs_paper,
        rationale="MVP 缩放配置（非原文设置）",
    )
    if bs_paper is not None and bs_paper != scaling.batch_size:
        deviations.append(
            Deviation(
                field_path="training_config.batch_size",
                paper_value=str(bs_paper),
                plan_value=str(scaling.batch_size),
                reason=f"MVP 缩放：固定批大小 {scaling.batch_size}",
            )
        )

    ep_field = card.hyperparameters.get("epochs")
    ep_paper = as_int(ep_field.value) if ep_field and ep_field.value is not None else None
    epochs_param = PlanParam(
        value=scaling.epochs,
        source=ParamSource.MVP_SCALING,
        paper_value=ep_paper,
        rationale="MVP 缩放配置（非原文设置）",
    )
    if ep_paper is not None and ep_paper != scaling.epochs:
        deviations.append(
            Deviation(
                field_path="training_config.epochs",
                paper_value=str(ep_paper),
                plan_value=str(scaling.epochs),
                reason=f"MVP 缩放：{scaling.epochs} epoch（原文 {ep_paper}）",
            )
        )
    return batch_param, epochs_param


def _preprocessing(card: ReproductionCard, assumptions: list[Assumption]) -> PreprocessingConfig:
    steps: list[PreprocessingStep] = []
    for key, field in card.data_preprocessing.items():
        if field.status is EvidenceStatus.MISSING or field.value is None:
            continue
        paper_backed = field.status in (
            EvidenceStatus.PAPER_EXPLICIT,
            EvidenceStatus.PAPER_DESCRIBED,
        )
        if paper_backed:
            source = ParamSource.PAPER_EXPLICIT
        else:
            source = inferred_source(field)
        steps.append(
            PreprocessingStep(
                name=key,
                detail=str(field.value),
                source=source,
                rationale=None if source is ParamSource.PAPER_EXPLICIT else "理解阶段推断/确认",
            )
        )
        if not paper_backed:
            # Step confirmation provenance lives on its corresponding assumption.
            assumptions.append(
                Assumption(
                    field_path=f"preprocessing_config.{key}",
                    kind=AssumptionKind.INFERRED,
                    value=str(field.value),
                    rationale="预处理步骤为推断值，论文未直接说明",
                    requires_confirmation=not field.human_confirmed,
                    human_confirmed=field.human_confirmed,
                    confirmed_by=confirmation_source(field),
                    confidence=field.confidence,
                    risk_tier=RiskTier.ROUTINE,
                    auto_acceptable=True,
                    card_status=field.status,
                    evidence_quote=field.evidence_quote,
                    source_page=field.source_page,
                )
            )
    if not steps:
        normalize_detail = (
            f"Normalize(mean={MNIST_NORMALIZE_MEAN}, std={MNIST_NORMALIZE_STD})（MNIST 全局统计量）"
        )
        steps = [
            PreprocessingStep(
                name="to_tensor",
                detail="图像转 torch.Tensor 并缩放至 [0,1]",
                source=ParamSource.MVP_SCALING,
                rationale="MVP 默认预处理",
            ),
            PreprocessingStep(
                name="normalize",
                detail=normalize_detail,
                source=ParamSource.MVP_SCALING,
                rationale="MVP 默认；论文未明确预处理",
            ),
        ]
        assumptions.append(
            Assumption(
                field_path="preprocessing_config",
                kind=AssumptionKind.MVP_SCALING,
                value="to_tensor + normalize",
                rationale="论文未明确预处理，采用 MNIST 标准 ToTensor+Normalize（非原文设置）",
                requires_confirmation=False,
                risk_tier=RiskTier.INFO,
                auto_acceptable=True,
                card_status=EvidenceStatus.MISSING,
            )
        )
    return PreprocessingConfig(steps=steps)


def build_plan(card: ReproductionCard, scaling: ScalingConfig | None = None) -> ExperimentPlan:
    _guard_lenet_mnist(card)
    scaling = scaling or ScalingConfig()
    assumptions: list[Assumption] = []
    deviations: list[Deviation] = []

    dataset_config = DatasetConfig(
        name="mnist",
        num_classes=MNIST_NUM_CLASSES,
        input_shape=list(MNIST_INPUT_SHAPE),
        train_subset_size=scaling.train_subset_size,
        test_subset_size=scaling.test_subset_size,
        full_train_size=MNIST_FULL_TRAIN,
        full_test_size=MNIST_FULL_TEST,
        source=(
            ParamSource.PAPER_EXPLICIT
            if card.dataset.status is EvidenceStatus.PAPER_EXPLICIT
            else ParamSource.INFERRED
        ),
        evidence_quote=card.dataset.evidence_quote,
        source_page=card.dataset.source_page,
    )
    if scaling.train_subset_size != MNIST_FULL_TRAIN:
        deviations.append(
            Deviation(
                field_path="dataset_config.train_subset_size",
                paper_value=str(MNIST_FULL_TRAIN),
                plan_value=str(scaling.train_subset_size),
                reason="MVP 缩放：训练子集而非全量 60000",
            )
        )

    model_cfg = ModelConfig(
        architecture="lenet-5",
        input_shape=list(MNIST_INPUT_SHAPE),
        num_classes=MNIST_NUM_CLASSES,
        description=str(card.model_architecture.value) if card.model_architecture.value else None,
        source=(
            ParamSource.PAPER_EXPLICIT
            if card.model_architecture.status is EvidenceStatus.PAPER_EXPLICIT
            else ParamSource.INFERRED
        ),
        evidence_quote=card.model_architecture.evidence_quote,
        source_page=card.model_architecture.source_page,
    )

    preprocessing_config = _preprocessing(card, assumptions)

    optimizer_param = _build_optimizer(card, assumptions)
    lr_field = card.hyperparameters.get("learning_rate") or EvidenceField(
        status=EvidenceStatus.MISSING
    )
    lr_param = param_from_evidence(
        lr_field,
        "training_config.learning_rate",
        fallback_value=0.01,
        fallback_rationale="论文未提供学习率；MVP 提出 0.01（SGD 常用初始值），需人工确认",
        cast=as_float,
        assumptions=assumptions,
    )
    loss_param = _build_loss(card, assumptions)
    wd_field = card.hyperparameters.get("weight_decay")
    wd_param = None
    if wd_field is not None:
        wd_param = param_from_evidence(
            wd_field,
            "training_config.weight_decay",
            fallback_value=0.0,
            fallback_rationale="论文未提供 weight_decay；MVP 提出 0.0（常规项，可批量采纳）",
            cast=as_float,
            assumptions=assumptions,
            risk_tier=RiskTier.ROUTINE,
            auto_acceptable=True,
        )
    batch_param, epochs_param = _scaling_params(card, scaling, deviations)
    training_config = TrainingConfig(
        optimizer=optimizer_param,
        learning_rate=lr_param,
        loss_function=loss_param,
        batch_size=batch_param,
        epochs=epochs_param,
        weight_decay=wd_param,
        seed=MVP_SEED,
    )

    metrics = [str(f.value) for f in card.evaluation_metrics if f.value is not None] or ["accuracy"]
    reported = card.reported_results[0] if card.reported_results else None
    evaluation_config = EvaluationConfig(
        metrics=metrics,
        target_metric=reported.metric if reported else metrics[0],
        reported_value=reported.value if reported else None,
        reported_condition=reported.condition if reported else None,
        reported_source_page=reported.source_page if reported else None,
        tolerance=MVP_TOLERANCE,
        comparison_note=(
            "MVP 在训练子集 / 少量 epoch 下运行，结果与原文全量训练不可直接对比；"
            "tolerance 仅作趋势性参考，非论文等价判定。"
        ),
    )

    runtime_budget = RuntimeBudget(
        max_seconds=scaling.max_seconds, device=MVP_DEVICE, seed=MVP_SEED
    )
    steps_per_epoch = math.ceil(scaling.train_subset_size / scaling.batch_size)
    total_steps = steps_per_epoch * scaling.epochs
    estimated = min(scaling.max_seconds, int(total_steps * 0.2 + 30))
    expected_runtime = ExpectedRuntime(
        estimated_seconds=estimated,
        basis=(
            f"LeNet-5 CPU：{scaling.train_subset_size} 训练子集 / batch {scaling.batch_size} "
            f"× {scaling.epochs} epoch ≈ {total_steps} 步，按 ~0.2s/步 + 30s 评估开销"
            f"保守估计（预算 {scaling.max_seconds}s）"
        ),
    )
    resource_estimates = ResourceEstimates(
        device=MVP_DEVICE,
        estimated_memory_mb=512,
        estimated_disk_mb=120,
        note=(
            "CPU 版 torch/torchvision；Runner 隔离无外网，MNIST 需预置于镜像/数据卷，不能在线下载。"
        ),
    )
    assumptions.append(
        Assumption(
            field_path="dataset_config",
            kind=AssumptionKind.MVP_SCALING,
            value="mnist (preloaded)",
            rationale="Runner 无外网出口，MNIST 需预置；代码生成/执行阶段须使用本地数据集路径。",
            requires_confirmation=False,
            risk_tier=RiskTier.INFO,
            auto_acceptable=True,
        )
    )

    n_confirm = sum(1 for a in assumptions if a.requires_confirmation and not a.human_confirmed)
    rationale = (
        "基于 Reproduction Card 的 LeNet-5 + MNIST MVP 缩放复现："
        f"训练子集 {scaling.train_subset_size}、"
        f"测试 {scaling.test_subset_size}、{scaling.epochs} epoch、batch {scaling.batch_size}、"
        f"seed {MVP_SEED}、CPU、预算 {scaling.max_seconds}s。缩放配置非原文设置"
        f"（见 deviations_from_paper）；{n_confirm} 项关键假设待人工确认（见 assumptions）。"
    )

    return ExperimentPlan(
        review_mode=card.review_mode,
        delegated_at=card.delegated_at,
        delegated_by=card.delegated_by,
        scaling_profile=SCALING_PROFILE,
        rationale=rationale,
        dataset_config=dataset_config,
        model_cfg=model_cfg,
        preprocessing_config=preprocessing_config,
        training_config=training_config,
        evaluation_config=evaluation_config,
        runtime_budget=runtime_budget,
        expected_runtime=expected_runtime,
        resource_estimates=resource_estimates,
        assumptions=assumptions,
        deviations_from_paper=deviations,
    )
