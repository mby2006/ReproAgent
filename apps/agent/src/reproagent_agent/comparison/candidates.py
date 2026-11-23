"""Find optimizer pairings the paper actually reports, deterministically.

Attribution is *source-local*: a metric is bound to the optimizer that appears in
the same table row (or the same sentence) as that metric, never matched by metric
name alone. Two tables that both claim "accuracy" therefore cannot leak into each
other -- a failure mode the acceptance tests exercise explicitly.
"""

import re
from dataclasses import dataclass, field
from itertools import combinations

from reproagent_schemas import (
    CandidateRequest,
    CandidateResponse,
    ComparisonCandidate,
    ComparisonGroupSpec,
    PaperReference,
)

from ..planning.assumptions import as_float
from .support import support_check

_NUMBER = re.compile(r"[-+]?\d*\.?\d+\s*%?")
_ALIASES = {
    "sgd": "sgd",
    "stochastic gradient descent": "sgd",
    "adam": "adam",
    "adamw": "adamw",
    "adagrad": "adagrad",
    "adadelta": "adadelta",
    "rmsprop": "rmsprop",
}
_METRIC_WORDS = {
    "accuracy": "accuracy",
    "acc": "accuracy",
    "top-1": "accuracy",
    "错误率": "error_rate",
    "error rate": "error_rate",
    "error": "error_rate",
    "loss": "test_loss",
    "损失": "test_loss",
}


@dataclass
class _Observation:
    optimizer: str
    label: str
    metric: str | None = None
    value: float | None = None
    learning_rate: float | None = None
    page: int | None = None
    section: str | None = None
    table_id: str | None = None
    quote: str | None = None
    condition: str | None = None
    source: str = "card"
    extra: dict = field(default_factory=dict)

    def reference(self) -> PaperReference:
        where = self.table_id or (f"p.{self.page}" if self.page else "原文")
        return PaperReference(
            label=f"{where} · {self.optimizer.upper()}",
            quote=self.quote,
            source_page=self.page,
            source_section=self.section,
            table_id=self.table_id,
            metric=self.metric,
            value=self.value,
            condition=self.condition,
        )


def _canonical(text: str) -> str | None:
    lowered = text.lower()
    for alias, canonical in _ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            return canonical
    return None


def _metric_in(text: str) -> str | None:
    lowered = text.lower()
    for word, metric in _METRIC_WORDS.items():
        if word in lowered:
            return metric
    return None


def _numbers(text: str) -> list[float]:
    values: list[float] = []
    for raw in _NUMBER.findall(text):
        percent = raw.strip().endswith("%")
        value = as_float(raw)
        if value is None:
            continue
        # A percentage result is stored as a fraction so 99% and 0.99 compare.
        values.append(value / 100 if percent and value > 1 else value)
    return values


def _rate_like(values: list[float]) -> float | None:
    for value in values:
        if 0 < value < 1e-2:  # learning-rate scale
            return value
    return None


def _result_like(values: list[float]) -> float | None:
    """A reported score, not a hyperparameter: prefer plausible accuracy values."""
    strong = [value for value in values if 0.5 <= value <= 1.0]
    if strong:
        return strong[-1]
    for value in values:
        if 0.01 <= value <= 1.0:
            return value
    return None


def _column_index(header: list[str], needles: tuple[str, ...]) -> int | None:
    for index, name in enumerate(header):
        if any(needle in name for needle in needles):
            return index
    return None


def _from_card(request: CandidateRequest) -> list[_Observation]:
    card = request.card
    optimizer = _canonical(str(card.optimizer.value or ""))
    if optimizer is None:
        return []
    lr_field = card.hyperparameters.get("learning_rate")
    learning_rate = as_float(lr_field.value) if lr_field is not None else None
    observed: list[_Observation] = []
    if not card.reported_results:
        observed.append(
            _Observation(
                optimizer=optimizer,
                label=optimizer.upper(),
                learning_rate=learning_rate,
                page=card.optimizer.source_page,
                section=card.optimizer.source_section,
                quote=card.optimizer.evidence_quote,
            )
        )
    for reported in card.reported_results:
        metric = _metric_in(str(reported.metric)) or str(reported.metric).lower()
        observed.append(
            _Observation(
                optimizer=optimizer,
                label=optimizer.upper(),
                metric=metric,
                value=reported.value,
                learning_rate=learning_rate,
                page=reported.source_page,
                section=reported.source_section,
                quote=reported.evidence_quote,
                condition=reported.condition,
                source="card",
            )
        )
    return observed


def _from_tables(document) -> list[_Observation]:
    observed: list[_Observation] = []
    for table in getattr(document, "tables", []) or []:
        caption = table.caption or ""
        header = [cell.lower() for cell in (table.rows[0] if table.rows else [])]
        lr_index = _column_index(header, ("lr", "learning rate", "learning_rate"))
        metric_index = _column_index(header, ("accuracy", "acc", "error", "loss"))
        for row in table.rows[1:]:
            joined = " | ".join(row)
            optimizer = _canonical(joined)
            if optimizer is None:
                continue
            metric = _metric_in(caption) or _metric_in(joined)
            learning_rate = (
                as_float(row[lr_index])
                if lr_index is not None and lr_index < len(row)
                else None
            )
            value = None
            if metric_index is not None and metric_index < len(row):
                cell_values = _numbers(row[metric_index])
                value = cell_values[0] if cell_values else None
            if value is None:
                numbers = [n for n in _numbers(joined) if n != learning_rate]
                value = _result_like(numbers)
            observed.append(
                _Observation(
                    optimizer=optimizer,
                    label=f"{optimizer.upper()} (表 {table.id})",
                    metric=metric,
                    value=value,
                    learning_rate=learning_rate,
                    page=table.page,
                    section=table.section,
                    table_id=table.id,
                    quote=joined[:300],
                    condition=caption or None,
                    source="table",
                )
            )
    return observed


def _from_blocks(document) -> list[_Observation]:
    observed: list[_Observation] = []
    for block in getattr(document, "blocks", []) or []:
        text = block.text
        if len(text) > 400:
            continue
        optimizer = _canonical(text)
        if optimizer is None:
            continue
        metric = _metric_in(text)
        if metric is None:
            continue
        numbers = _numbers(text)
        value = _result_like(numbers)
        if value is None:
            continue
        observed.append(
            _Observation(
                optimizer=optimizer,
                label=f"{optimizer.upper()} (正文)",
                metric=metric,
                value=value,
                learning_rate=_rate_like(numbers),
                page=block.page,
                section=block.section,
                quote=text[:300],
                source="text",
            )
        )
    return observed


def _best_by_optimizer(observations: list[_Observation]) -> dict[str, _Observation]:
    best: dict[str, _Observation] = {}
    rank = {"table": 0, "text": 1, "card": 2}
    for obs in observations:
        current = best.get(obs.optimizer)
        if current is None:
            best[obs.optimizer] = obs
            continue
        if obs.value is not None and current.value is None:
            best[obs.optimizer] = obs
        elif obs.value is not None and current.value is not None:
            if rank[obs.source] < rank[current.source]:
                best[obs.optimizer] = obs
    return best


def _duplicate_metric_note(observations: list[_Observation]) -> str | None:
    """Warn when the same metric name appears in several tables.

    Attribution stays row-local, but the reader must still be told that a
    same-named number exists elsewhere so they can verify the table they picked.
    """
    by_metric: dict[str, set[str]] = {}
    for obs in observations:
        if obs.metric and obs.table_id:
            by_metric.setdefault(obs.metric, set()).add(obs.table_id)
    flagged = {metric: tables for metric, tables in by_metric.items() if len(tables) > 1}
    if not flagged:
        return None
    detail = "；".join(
        f"{metric} 出现在 {', '.join(sorted(tables))}" for metric, tables in sorted(flagged.items())
    )
    return (
        f"多张表格出现同名指标（{detail}）；"
        "本系统按「同一行内的优化器」归属结果，请核对页码与表号后再选择。"
    )


def find_candidates(request: CandidateRequest) -> CandidateResponse:
    card_observations = _from_card(request)
    table_observations = _from_tables(request.document)
    text_observations = _from_blocks(request.document)
    best = _best_by_optimizer(card_observations + table_observations + text_observations)
    note = _duplicate_metric_note(table_observations)

    candidates: list[ComparisonCandidate] = []
    if len(best) < 2:
        only = next(iter(best.values()), None)
        candidates.append(
            ComparisonCandidate(
                id="candidate-insufficient",
                label="未找到可对照的第二组优化器",
                group_optimizers=[only.optimizer if only else "sgd", "adam"],
                metric=only.metric if only else None,
                references=[only.reference()] if only else [],
                comparable=False,
                blocking=["论文中没有第二组优化器的可核验结果"],
                note="需要在原文中同时找到两组的优化器设置与结果，才可建立对照。",
            )
        )
        groups = [
            ComparisonGroupSpec(key="a", label="A 组", optimizer="sgd"),
            ComparisonGroupSpec(key="b", label="B 组", optimizer="adam"),
        ]
        return CandidateResponse(
            candidates=candidates,
            support=support_check(card=request.card, groups=groups),
            paper_references=[],
        )

    optimizers = sorted(best)
    support = None
    for index, (left, right) in enumerate(combinations(optimizers, 2)):
        a, b = best[left], best[right]
        metric = a.metric if a.metric == b.metric else (a.metric or b.metric)
        references = [a.reference(), b.reference()]
        blocking: list[str] = []
        if a.value is None or b.value is None:
            blocking.append("至少一组缺少可核验的数值结果")
        groups = [
            ComparisonGroupSpec(key="a", label=f"{left.upper()} 组", optimizer=left,
                                 learning_rate=a.learning_rate,
                                 references=[a.reference()]),
            ComparisonGroupSpec(key="b", label=f"{right.upper()} 组", optimizer=right,
                                 learning_rate=b.learning_rate,
                                 references=[b.reference()]),
        ]
        pair_support = support_check(
            card=request.card, groups=groups, target_metric=metric,
        )
        if index == 0:
            support = pair_support
        if not pair_support.ok:
            blocking.extend(gap.detail for gap in pair_support.missing)
        candidates.append(
            ComparisonCandidate(
                id=f"candidate-{left}-vs-{right}",
                label=f"{left.upper()} vs {right.upper()}",
                group_optimizers=[left, right],
                metric=metric,
                references=references,
                comparable=not blocking,
                blocking=blocking,
                note=note,
            )
        )

    references = list({ref.label: ref for ref in (a.reference() for a in best.values())}.values())
    return CandidateResponse(
        candidates=candidates, support=support, paper_references=references
    )
