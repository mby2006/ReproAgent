"""What the platform can actually answer today.

The product promise is narrow on purpose: MNIST + LeNet-5 + two built-in
optimizers + accuracy-family metrics. Anything else must fail *here*, on the
upload page, naming the exact missing capability -- not halfway through a run.
"""

from reproagent_schemas import (
    ComparisonGroupSpec,
    EvidenceStatus,
    ReproductionCard,
    SupportCheck,
    SupportGap,
)

from ..planning.assumptions import normalize_loss, normalize_optimizer

SUPPORTED_METRICS = {"accuracy", "error_rate", "test_loss"}
SUPPORTED_DATASET = "mnist"
SUPPORTED_ARCHITECTURE = "lenet"

MAX_TRAIN_SAMPLES = 60_000
MAX_TEST_SAMPLES = 10_000
MAX_EPOCHS = 20
MAX_SECONDS = 600


def _card_metric(card: ReproductionCard) -> tuple[str, list[str]]:
    reported = card.reported_results[0].metric if card.reported_results else None
    metrics = [
        str(field.value).strip().lower().replace(" ", "_")
        for field in card.evaluation_metrics
        if field.value is not None
    ]
    if reported:
        metrics.insert(0, str(reported).strip().lower().replace(" ", "_"))
    return (reported or (metrics[0] if metrics else "")), metrics


def support_check(
    card: ReproductionCard,
    groups: list[ComparisonGroupSpec],
    *,
    target_metric: str | None = None,
    max_seconds: int | None = None,
) -> SupportCheck:
    gaps: list[SupportGap] = []
    supported: list[str] = []

    dataset = str(card.dataset.value or "").lower()
    if card.dataset.status is EvidenceStatus.MISSING or SUPPORTED_DATASET not in dataset:
        gaps.append(
            SupportGap(
                field="dataset",
                detail=f"论文数据集为「{card.dataset.value or '未说明'}」，不是 MNIST",
                expectation="当前仅支持 MNIST 数据集",
            )
        )
    else:
        supported.append("数据集：MNIST")

    arch = f"{card.model_architecture.value or ''} {card.method.value or ''}".lower()
    if SUPPORTED_ARCHITECTURE not in arch:
        gaps.append(
            SupportGap(
                field="model_architecture",
                detail=f"论文模型为「{card.model_architecture.value or '未说明'}」，不是 LeNet-5",
                expectation="当前仅支持现有 LeNet-5 模板",
            )
        )
    else:
        supported.append("模型：LeNet-5 模板")

    optimizers: list[str] = []
    for group in groups:
        normalized = normalize_optimizer(group.optimizer)
        if normalized is None:
            gaps.append(
                SupportGap(
                    field=f"groups.{group.key.value}.optimizer",
                    detail=f"优化器「{group.optimizer}」不在已支持列表",
                    expectation="支持 SGD / Adam / AdamW / Adagrad / Adadelta / RMSprop",
                )
            )
        else:
            optimizers.append(normalized)
    if len(optimizers) == 2 and len(set(optimizers)) == 1:
        gaps.append(
            SupportGap(
                field="groups",
                detail="两组的优化器相同，无法构成对照",
                expectation="两组必须使用不同的优化器",
            )
        )
    elif len(optimizers) == 2:
        supported.append(f"优化器：{' vs '.join(optimizers)}")

    metric_raw = (target_metric or _card_metric(card)[0] or "").strip().lower()
    metric = metric_raw.replace(" ", "_")
    if metric not in SUPPORTED_METRICS and metric not in {"error_rate", "test_accuracy"}:
        gaps.append(
            SupportGap(
                field="evaluation_metrics",
                detail=f"主指标「{metric_raw or '未说明'}」无法直接比较",
                expectation="主指标需为准确率、错误率或测试损失",
            )
        )
    else:
        supported.append(f"主指标：{metric}")

    if normalize_loss(card.loss_function.value) is None:
        gaps.append(
            SupportGap(
                field="loss_function",
                detail=f"损失函数「{card.loss_function.value or '未说明'}」不是交叉熵",
                expectation="现有训练模板只实现交叉熵损失",
            )
        )
    else:
        supported.append("损失：交叉熵")

    if max_seconds is not None and max_seconds > MAX_SECONDS:
        gaps.append(
            SupportGap(
                field="runtime_budget.max_seconds",
                detail=f"预算 {max_seconds}s 超过单次运行上限 {MAX_SECONDS}s",
                expectation=f"单次运行不超过 {MAX_SECONDS}s",
            )
        )

    return SupportCheck(ok=not gaps, supported=supported, missing=gaps)
