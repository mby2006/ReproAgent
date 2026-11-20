"""Result Comparison: paper-reported values (grounded evidence) vs real run metrics.

Pure arithmetic; no LLM. Paper values come ONLY from card.reported_results
(evidence-grounded floats); experiment values ONLY from the run's metrics.json.
Percentages and error rates are normalized to accuracy fractions deterministically.
A within-tolerance match is a trend observation, never a full-reproduction claim.
"""

from reproagent_schemas import (
    CheckStatus,
    EvidenceRef,
    EvidenceStatus,
    MetricComparison,
    ReproductionCard,
    RunStatus,
)
from reproagent_schemas.plan import ExperimentPlan

_ACCURACY_NAMES = {"accuracy", "test accuracy", "train accuracy", "top-1 accuracy", "top1 accuracy"}
_ERROR_NAMES = {"error", "error rate", "test error", "test error rate", "misclassification rate"}
_LOSS_NAMES = {"loss", "test loss", "test_loss", "cross entropy loss"}
_RUNTIME_NAMES = {"runtime", "runtime seconds", "training time", "time"}


def _classify_metric(name: str) -> str | None:
    key = name.strip().lower().replace("_", " ")
    if key in _ACCURACY_NAMES:
        return "accuracy"
    if key in _ERROR_NAMES:
        return "error_rate"
    if key in _LOSS_NAMES:
        return "test_loss"
    if key in _RUNTIME_NAMES:
        return "runtime_seconds"
    return None


def _to_fraction(value: float) -> float:
    return value / 100.0 if value > 1.0 else value


def _paper_accuracy(result) -> tuple[float, str] | None:
    """Normalize a reported accuracy/error-rate value to an accuracy fraction.

    Returns None when an error rate is ambiguous: a value in (0, 1] with no
    percent sign in the quote could be either a percentage (0.8 -> 0.8%) or a
    fraction (0.8 -> 80% error). Guessing either way can fabricate a match, so
    the caller must treat it as unusable evidence rather than normalize it.
    """
    value = float(result.value)
    kind = _classify_metric(result.metric)
    quote = (result.evidence_quote or "").lower()
    percent_hint = "%" in quote or "percent" in quote
    if kind == "error_rate":
        if 0 < value <= 1 and not percent_hint:
            return None
        fraction = value / 100.0 if value > 1.0 or percent_hint else value
        return 1.0 - fraction, f"原文报告错误率 {value}，换算为 accuracy = 1 - {fraction}"
    fraction = _to_fraction(value)
    note = f"原文报告 accuracy {value}" + ("（百分制，换算为小数）" if value > 1.0 else "")
    return fraction, note


def compare_results(
    card: ReproductionCard,
    plan: ExperimentPlan,
    metrics: dict,
    run_status: RunStatus,
) -> list[MetricComparison]:
    tolerance = plan.evaluation_config.tolerance
    succeeded = run_status is RunStatus.SUCCEEDED

    paper_accuracy: list[tuple[float, EvidenceRef, str]] = []
    ambiguous_errors: list[tuple[float, EvidenceRef]] = []
    paper_other: dict[str, tuple[float, EvidenceRef]] = {}
    for result in card.reported_results:
        if result.value is None or result.status not in (
            EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED,
        ):
            continue
        ref = EvidenceRef(
            kind="paper",
            quote=result.evidence_quote,
            source_page=result.source_page,
            source_section=result.source_section,
            detail=f"{result.metric}={result.value}"
            + (f" ({result.condition})" if result.condition else ""),
        )
        kind = _classify_metric(result.metric)
        if kind in ("accuracy", "error_rate"):
            normalized = _paper_accuracy(result)
            if normalized is None:
                ambiguous_errors.append((float(result.value), ref))
            else:
                value, note = normalized
                paper_accuracy.append((value, ref, note))
        elif kind in ("test_loss", "runtime_seconds"):
            paper_other[kind] = (float(result.value), ref)

    comparisons: list[MetricComparison] = []

    def _experiment_value(key: str) -> float | None:
        if not succeeded:
            return None
        value = metrics.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    def _build(
        metric: str,
        unit: str,
        paper_value: float | None,
        experiment_value: float | None,
        paper_ref: EvidenceRef | None,
        apply_tolerance: bool,
        note: str | None,
    ) -> MetricComparison:
        abs_diff = rel_diff = pp_diff = None
        if paper_value is not None and experiment_value is not None:
            abs_diff = abs(experiment_value - paper_value)
            rel_diff = abs_diff / abs(paper_value) if paper_value != 0 else None
            if metric == "accuracy":
                pp_diff = (experiment_value - paper_value) * 100.0
        if paper_value is None:
            status = CheckStatus.NOT_APPLICABLE
            note = note or "论文证据未报告该指标，无法对比（仅记录实验值）"
        elif experiment_value is None:
            status = CheckStatus.UNKNOWN
            note = note or "真实运行未产出该指标（运行未成功或指标缺失）"
        elif apply_tolerance:
            within = abs_diff is not None and abs_diff <= tolerance
            status = CheckStatus.MATCHED if within else CheckStatus.DEVIATED
            note = (
                f"绝对差 {abs_diff:.4f} ≤ tolerance {tolerance}（仅趋势参考，非论文等价判定）"
                if within
                else f"绝对差 {abs_diff:.4f} > tolerance {tolerance}；缩放实验下不构成复现失败定论"
            )
        else:
            status = CheckStatus.UNKNOWN
            note = note or "已记录差值；该指标未定义验收容差，不做匹配判定"
        return MetricComparison(
            metric=metric,
            paper_value=paper_value,
            experiment_value=experiment_value,
            absolute_difference=abs_diff,
            relative_difference=rel_diff,
            percentage_point_difference=pp_diff,
            unit=unit,
            comparison_status=status,
            tolerance=tolerance if apply_tolerance else None,
            paper_evidence=paper_ref,
            experiment_evidence=(
                EvidenceRef(
                    kind="metrics",
                    field_path=f"metrics_json.{metric}",
                    detail=str(experiment_value),
                )
                if experiment_value is not None
                else None
            ),
            note=note,
        )

    def _ambiguous(metric: str) -> MetricComparison:
        raw = "、".join(f"{v:g}" for v, _ in ambiguous_errors)
        comp = _build(
            metric,
            "fraction",
            None,
            _experiment_value("accuracy"),
            ambiguous_errors[0][1],
            apply_tolerance=True,
            note=(
                f"原文报告错误率 {raw} 未标注百分号，无法确定是百分比还是小数；"
                "为避免误判不做换算，目标指标对比证据不足"
            ),
        )
        comp.comparison_status = CheckStatus.UNKNOWN
        return comp

    if paper_accuracy:
        for index, (value, ref, conversion_note) in enumerate(paper_accuracy):
            metric = "accuracy" if index == 0 else f"accuracy_{index + 1}"
            comparisons.append(
                _build(
                    metric,
                    "fraction",
                    value,
                    _experiment_value("accuracy"),
                    ref,
                    apply_tolerance=True,
                    note=None if index == 0 else conversion_note,
                )
            )
            if index == 0 and conversion_note.startswith("原文报告错误率"):
                comparisons[-1].note = (comparisons[-1].note or "") + "；" + conversion_note
        if ambiguous_errors:
            comparisons.append(_ambiguous("accuracy_ambiguous"))
    elif ambiguous_errors:
        comparisons.append(_ambiguous("accuracy"))
    else:
        comparisons.append(
            _build(
                "accuracy",
                "fraction",
                None,
                _experiment_value("accuracy"),
                None,
                apply_tolerance=True,
                note="论文证据未报告 accuracy/错误率，目标指标对比证据不足",
            )
        )

    comparisons.append(
        _build(
            "test_loss",
            "nats",
            paper_other.get("test_loss", (None, None))[0],
            _experiment_value("test_loss"),
            paper_other.get("test_loss", (None, None))[1],
            apply_tolerance=False,
            note=None,
        )
    )
    comparisons.append(
        _build(
            "runtime_seconds",
            "seconds",
            paper_other.get("runtime_seconds", (None, None))[0],
            _experiment_value("runtime_seconds"),
            paper_other.get("runtime_seconds", (None, None))[1],
            apply_tolerance=False,
            note=None,
        )
    )

    known = {"accuracy", "error_rate", "test_loss", "runtime_seconds"}
    for result in card.reported_results:
        kind = _classify_metric(result.metric)
        if (
            kind is None or kind in known or result.value is None
            or result.status not in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        ):
            continue
        known.add(kind)
        comparisons.append(
            _build(
                kind,
                "reported",
                float(result.value),
                _experiment_value(kind),
                EvidenceRef(
                    kind="paper",
                    quote=result.evidence_quote,
                    source_page=result.source_page,
                    detail=f"{result.metric}={result.value}",
                ),
                apply_tolerance=False,
                note=None,
            )
        )
    return comparisons