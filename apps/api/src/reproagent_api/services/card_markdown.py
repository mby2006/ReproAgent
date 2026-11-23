"""Deterministic Markdown export of a Reproduction Card.

Pure rendering: no LLM, no database access. Every value, evidence quote, page
and status in the output is copied from the persisted card version, so the
exported document can be audited against the paper by hand. The renderer never
invents provenance: a field without evidence is rendered as such.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from reproagent_schemas import (
    ConfirmationSource,
    EvidenceField,
    EvidenceStatus,
    ReproductionCard,
    ReviewMode,
    SpecOrigin,
)

_STATUS_LABEL = {
    EvidenceStatus.PAPER_EXPLICIT: "原文",
    EvidenceStatus.PAPER_DESCRIBED: "原文描述",
    EvidenceStatus.INFERRED: "推断",
    EvidenceStatus.MISSING: "缺失",
}

_SOURCE_LABEL = {
    ConfirmationSource.HUMAN: "人工逐项确认",
    ConfirmationSource.AI_ASSISTED: "采纳 AI 推荐",
    ConfirmationSource.AUTO: "托管自动接受",
}

_MODE_LABEL = {
    ReviewMode.EXPERT: "专家模式（人工逐项核验）",
    ReviewMode.ASSISTED: "辅助模式（AI 推荐 + 人工决策）",
    ReviewMode.DELEGATED: "托管模式（未逐项核验）",
}

_ORIGIN_LABEL = {
    SpecOrigin.EXTRACTION: "解析提取",
    SpecOrigin.USER_EDIT: "用户修改",
}


def _cell(text: Any) -> str:
    """Collapse whitespace and escape pipes so a value cannot break a table."""
    collapsed = " ".join(str(text).split())
    return collapsed.replace("|", "\\|")


def _label(mapping: dict[Any, str], value: Any) -> str:
    """Enum label with a fallback that also works for raw strings from the DB."""
    return mapping.get(value) or getattr(value, "value", str(value))


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return _cell(value)


def _status(field: EvidenceField[Any]) -> str:
    return f"`{field.status.value}` {_STATUS_LABEL.get(field.status, '')}".strip()


def _location(field: EvidenceField[Any]) -> str:
    parts: list[str] = []
    if field.source_page is not None:
        parts.append(f"第 {field.source_page} 页")
    if field.source_section:
        parts.append(field.source_section)
    return " · ".join(parts) if parts else "—"


def _confirmation(field: EvidenceField[Any]) -> str:
    if field.confirmed_by is not None:
        return _label(_SOURCE_LABEL, field.confirmed_by)
    if field.human_confirmed:
        return "已确认，来源未知"
    return "未确认"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def iter_fields(card: ReproductionCard) -> list[tuple[str, EvidenceField[Any]]]:
    """Every evidence field in the card, paired with its stable field path."""
    fields: list[tuple[str, EvidenceField[Any]]] = [
        (name, getattr(card, name))
        for name in ("method", "model_architecture", "dataset", "optimizer", "loss_function")
    ]
    fields.extend((f"data_preprocessing.{k}", v) for k, v in card.data_preprocessing.items())
    fields.extend((f"hyperparameters.{k}", v) for k, v in card.hyperparameters.items())
    fields.extend(
        (f"evaluation_metrics[{i}]", v) for i, v in enumerate(card.evaluation_metrics)
    )
    fields.extend(
        (f"reported_results[{i}]", v) for i, v in enumerate(card.reported_results)
    )
    fields.extend((f"baselines[{i}]", v) for i, v in enumerate(card.baselines))
    return fields


def _core_section(title: str, field: EvidenceField[Any]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"- **值**：{_value(field.value)}",
        f"- **证据**：{_status(field)} · {_location(field)}",
    ]
    if field.status is EvidenceStatus.INFERRED:
        lines.append(f"- **置信度**：{field.confidence:.2f} · **确认**：{_confirmation(field)}")
    else:
        lines.append(f"- **确认**：{_confirmation(field)}")
    if field.evidence_quote:
        quote = " ".join(field.evidence_quote.split())
        lines.extend(["", f"> {quote}"])
    lines.append("")
    return lines


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    lines.append("")
    return lines


def _field_rows(fields: list[tuple[str, EvidenceField[Any]]]) -> list[list[str]]:
    return [
        [
            f"`{path}`",
            _value(field.value),
            _status(field),
            _location(field),
            _confirmation(field),
        ]
        for path, field in fields
    ]


def render_card_markdown(
    card: ReproductionCard,
    *,
    paper_title: str,
    paper_id: UUID,
    version: int,
    version_id: UUID,
    origin: SpecOrigin,
    created_at: datetime | None,
) -> str:
    """Render one persisted Card version as a self-contained Markdown document."""
    fields = iter_fields(card)
    pending = [
        (path, field)
        for path, field in fields
        if field.status in (EvidenceStatus.INFERRED, EvidenceStatus.MISSING)
        and not (field.human_confirmed or field.confirmed_by is not None)
    ]
    explicit = [
        path
        for path, field in fields
        if field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
    ]
    inferred = [p for p, f in fields if f.status is EvidenceStatus.INFERRED]
    missing = [p for p, f in fields if f.status is EvidenceStatus.MISSING]

    lines: list[str] = [f"# {_cell(paper_title) or '论文复现卡片'}", ""]
    lines.extend(
        [
            f"> **Reproduction Card v{version}** · 来源："
            f"{_label(_ORIGIN_LABEL, origin)} · 生成于 {_format_time(created_at)}",
            f"> 论文 ID `{paper_id}` · 版本 ID `{version_id}`",
            f"> 审核模式：{_label(_MODE_LABEL, card.review_mode)}",
            f"> 证据字段 {len(fields)} 个 · 原文 {len(explicit)} 个 · "
            f"推断 {len(inferred)} 个 · 缺失 {len(missing)} 个 · 待确认 {len(pending)} 项",
            "",
        ]
    )
    if card.review_mode is ReviewMode.DELEGATED:
        lines.extend(
            [
                "> **声明**：本版本的待确认项由用户显式授权系统代为接受，"
                f"未经人工逐项核验（授权于 {_format_time(card.delegated_at)}）。",
                "",
            ]
        )

    lines.extend(["## 1. 核心字段", ""])
    lines.extend(_core_section("方法", card.method))
    lines.extend(_core_section("模型架构", card.model_architecture))
    lines.extend(_core_section("数据集", card.dataset))
    lines.extend(_core_section("优化器", card.optimizer))
    lines.extend(_core_section("损失函数", card.loss_function))

    lines.extend(["## 2. 数据预处理", ""])
    if card.data_preprocessing:
        rows = _field_rows(
            [(k, v) for k, v in card.data_preprocessing.items()]
        )
        lines.extend(_table(["字段", "值", "证据", "位置", "确认"], rows))
    else:
        lines.extend(["（论文未提取到预处理步骤）", ""])

    lines.extend(["## 3. 超参数", ""])
    if card.hyperparameters:
        rows = _field_rows([(k, v) for k, v in card.hyperparameters.items()])
        lines.extend(_table(["字段", "值", "证据", "位置", "确认"], rows))
    else:
        lines.extend(["（论文未提取到超参数）", ""])

    lines.extend(["## 4. 评估指标", ""])
    if card.evaluation_metrics:
        lines.extend(
            _table(
                ["指标", "证据", "位置", "确认"],
                [
                    [_value(f.value), _status(f), _location(f), _confirmation(f)]
                    for f in card.evaluation_metrics
                ],
            )
        )
    else:
        lines.extend(["（论文未提取到评估指标）", ""])

    lines.extend(["## 5. 论文报告结果", ""])
    if card.reported_results:
        lines.extend(
            _table(
                ["指标", "条件", "值", "证据", "位置"],
                [
                    [
                        _value(r.metric),
                        _value(r.condition),
                        _value(r.value),
                        _status(r),
                        _location(r),
                    ]
                    for r in card.reported_results
                ],
            )
        )
    else:
        lines.extend(["（论文未提取到报告结果）", ""])

    lines.extend(["## 6. 对比基线", ""])
    if card.baselines:
        lines.extend(
            _table(
                ["基线", "描述", "证据", "位置"],
                [
                    [_value(b.name), _value(b.value), _status(b), _location(b)]
                    for b in card.baselines
                ],
            )
        )
    else:
        lines.extend(["（论文未提取到对比基线）", ""])

    lines.extend(["## 7. 缺失信息", ""])
    if card.missing_information:
        lines.extend(f"- {_cell(item)}" for item in card.missing_information)
        lines.append("")
    else:
        lines.extend(["（无）", ""])

    lines.extend(["## 8. 待确认字段（推断 / 缺失）", ""])
    if pending:
        lines.extend(
            _table(
                ["字段", "值", "证据", "位置"],
                [
                    [f"`{path}`", _value(field.value), _status(field), _location(field)]
                    for path, field in pending
                ],
            )
        )
    else:
        lines.extend(["（无需要人工确认的推断/缺失字段）", ""])

    lines.extend(["## 9. 审核与溯源", ""])
    lines.append(f"- 审核模式：{_label(_MODE_LABEL, card.review_mode)}")
    if card.review_selection_threshold is not None:
        lines.append(
            f"- 高置信预选阈值：{card.review_selection_threshold:.2f}"
        )
    if card.delegated_at is not None:
        lines.append(f"- 托管授权时间：{_format_time(card.delegated_at)}")
        lines.append(f"- 托管授权人：`{card.delegated_by}`")
    lines.append(f"- 审核决策记录：{len(card.review_decisions)} 条")
    lines.append("")
    if card.review_decisions:
        lines.extend(
            _table(
                ["字段", "变更", "确认来源", "模式", "时间"],
                [
                    [
                        f"`{_cell(d.field_path)}`",
                        f"{_value(d.before)} → {_value(d.after)}",
                        _SOURCE_LABEL.get(d.confirmed_by, "—")
                        if d.confirmed_by
                        else "—",
                        d.review_mode.value if d.review_mode else "—",
                        _format_time(d.timestamp),
                    ]
                    for d in card.review_decisions
                ],
            )
        )

    lines.extend(
        [
            "---",
            "",
            "## 证据等级说明",
            "",
            "| 等级 | 含义 |",
            "|---|---|",
            "| `paper_explicit` | 论文原文逐字陈述，附页码与引文 |",
            "| `paper_described` | 论文以文字描述了该做法，非逐字数值 |",
            "| `inferred` | 论文未直接说明，由系统推断，需人工确认 |",
            "| `missing` | 论文未提供，系统不猜测 |",
            "",
            "> 本文档由 ReproAgent 依据已持久化的 Card 版本确定性生成，生成过程不调用 LLM。",
            "> 引文与页码由 PDF 解析器回填并做归一化匹配，LLM 无法自行指定来源；"
            "无法在原文定位的引文会被降级为 `inferred` 或 `missing`。",
            "",
        ]
    )
    return "\n".join(lines)
