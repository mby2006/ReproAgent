"""Method Validation: paper evidence (Card) vs implementation facts (pinned code).

Deterministic keyword/number matching over grounded evidence only. When the
paper side lacks evidence for an item the check is `unknown` -- never guessed.
"""

import re

from reproagent_schemas import (
    CheckStatus,
    EvidenceRef,
    MethodCheck,
    MethodItem,
    ReproductionCard,
)
from reproagent_schemas.evidence import EvidenceField, EvidenceStatus

from .facts import ImplementationFacts

_LOSS_ALIASES = {
    "cross entropy": "cross_entropy",
    "cross-entropy": "cross_entropy",
    "cross_entropy": "cross_entropy",
    "softmax loss": "cross_entropy",
    "softmax cross entropy": "cross_entropy",
    "mse": "mse",
    "mean squared error": "mse",
    "l1": "l1",
    "nll": "nll",
    "negative log likelihood": "nll",
    "bce": "bce",
    "binary cross entropy": "bce",
}

_ACTIVATION_KEYWORDS = {
    "relu": "relu",
    "rectified": "relu",
    "tanh": "tanh",
    "sigmoid": "sigmoid",
    "gelu": "gelu",
}


def _paper_ref(field: EvidenceField | None) -> EvidenceRef | None:
    if (
        field is None or field.value is None
        or field.status not in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
    ):
        return None
    return EvidenceRef(
        kind="paper",
        quote=field.evidence_quote,
        source_page=field.source_page,
        source_section=field.source_section,
        detail=str(field.value),
    )


def _code_ref(facts: ImplementationFacts, detail: str) -> EvidenceRef | None:
    path = facts.model_source or facts.train_source or facts.config_source
    if path is None:
        return None
    return EvidenceRef(kind="code", file_path=path, detail=detail)


def _evidence_text(card: ReproductionCard) -> str:
    parts: list[str] = []
    for field in (card.model_architecture, card.method):
        if _paper_ref(field) is None:
            continue
        if field.value is not None:
            parts.append(str(field.value))
        if field.evidence_quote:
            parts.append(field.evidence_quote)
    return " ".join(parts).lower()


def _impl_summary(facts: ImplementationFacts) -> str:
    convs = ", ".join(
        f"conv {c.in_channels}->{c.out_channels} (k{c.kernel_size}, p{c.padding})"
        for c in facts.conv_layers
    )
    linears = ", ".join(
        f"linear {layer.in_features}->{layer.out_features}" for layer in facts.linear_layers
    )
    pools = ", ".join(f"{p.kind}pool k{p.kernel_size}/s{p.stride}" for p in facts.pooling)
    activations = "/".join(sorted(set(facts.activations))) or "none"
    return f"{convs}; {pools}; {linears}; activation={activations}"


def _check_architecture(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    text = _evidence_text(card)
    ref = _paper_ref(card.model_architecture) or _paper_ref(card.method)
    impl = facts.architecture
    summary = f"architecture={impl}" if impl else "architecture unavailable"
    if impl is None:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取架构事实"
    elif ref is None:
        status, note = CheckStatus.UNKNOWN, "Card 未提供架构的原文证据"
    elif "lenet" in text:
        if impl == "lenet-5":
            status, note = CheckStatus.MATCHED, "原文证据指向 LeNet，实现为 lenet-5"
        else:
            status, note = CheckStatus.DEVIATED, f"原文指向 LeNet，实现为 {impl}"
    else:
        status, note = CheckStatus.UNKNOWN, "原文证据未包含可识别的架构名，无法比对"
    return MethodCheck(
        item=MethodItem.ARCHITECTURE,
        status=status,
        paper_value=ref.detail if ref else None,
        implementation_value=impl,
        paper_evidence=ref,
        implementation_evidence=_code_ref(facts, summary),
        note=note,
    )


def _check_layers(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    text = _evidence_text(card)
    counts = [int(m) for m in re.findall(r"(\d+)\s*(?:feature maps?|channels?|filters?)", text)]
    counts += [
        int(m)
        for m in re.findall(r"(?:feature maps?|channels?|filters?)\s*(?:of\s*)?(\d+)", text)
    ]
    impl_counts = [c.out_channels for c in facts.conv_layers]
    detail = _impl_summary(facts)
    if not facts.conv_layers:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取卷积层配置"
    elif not counts:
        status, note = (
            CheckStatus.UNKNOWN,
            "原文证据未给出通道/特征图数量，无法比对层配置（实现事实已记录）",
        )
    elif sorted(set(counts)) == sorted(set(impl_counts)):
        status, note = CheckStatus.MATCHED, f"原文通道数 {sorted(set(counts))} 与实现一致"
    else:
        status, note = (
            CheckStatus.DEVIATED,
            f"原文通道数 {sorted(set(counts))} 与实现 {impl_counts} 不一致",
        )
    return MethodCheck(
        item=MethodItem.LAYER_CONFIGURATION,
        status=status,
        paper_value=", ".join(str(c) for c in counts) if counts else None,
        implementation_value=", ".join(str(c) for c in impl_counts) if impl_counts else None,
        paper_evidence=_paper_ref(card.model_architecture),
        implementation_evidence=_code_ref(facts, detail),
        note=note,
    )


def _check_activation(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    text = _evidence_text(card)
    found = {canon for keyword, canon in _ACTIVATION_KEYWORDS.items() if keyword in text}
    impl = sorted(set(facts.activations))
    if not facts.activations:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取激活函数"
    elif not found:
        status, note = CheckStatus.UNKNOWN, "原文证据未明确激活函数，无法比对"
    elif found == set(impl):
        status, note = CheckStatus.MATCHED, f"原文激活 {sorted(found)} 与实现一致"
    else:
        status, note = (
            CheckStatus.DEVIATED,
            f"原文激活 {sorted(found)} 与实现 {impl} 不一致",
        )
    return MethodCheck(
        item=MethodItem.ACTIVATION,
        status=status,
        paper_value=", ".join(sorted(found)) if found else None,
        implementation_value=", ".join(impl) if impl else None,
        paper_evidence=_paper_ref(card.model_architecture) or _paper_ref(card.method),
        implementation_evidence=_code_ref(facts, f"activations={impl}"),
        note=note,
    )


def _check_subsampling(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    text = _evidence_text(card)
    impl_kinds = sorted({p.kind for p in facts.pooling})
    detail = ", ".join(f"{p.kind}pool k{p.kernel_size}/s{p.stride}" for p in facts.pooling)
    explicit = None
    if re.search(r"max[\s-]?pool", text):
        explicit = "max"
    elif re.search(r"(?:average|avg)[\s-]?pool", text):
        explicit = "average"
    generic = bool(re.search(r"pool|sub[\s-]?sampl|down[\s-]?sampl", text))
    if not facts.pooling:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取池化/下采样事实"
    elif explicit is not None:
        if explicit in impl_kinds:
            status, note = CheckStatus.MATCHED, f"原文明确 {explicit} pooling，与实现一致"
        else:
            status, note = (
                CheckStatus.DEVIATED,
                f"原文明确 {explicit} pooling，实现为 {impl_kinds}",
            )
    elif generic:
        status, note = CheckStatus.MATCHED, "原文提及下采样/池化，实现含池化层（类型未明确）"
    else:
        status, note = CheckStatus.UNKNOWN, "原文证据未提及下采样/池化，无法比对"
    return MethodCheck(
        item=MethodItem.SUBSAMPLING,
        status=status,
        paper_value=explicit or ("subsampling (unspecified)" if generic else None),
        implementation_value=detail or None,
        paper_evidence=_paper_ref(card.model_architecture) or _paper_ref(card.method),
        implementation_evidence=_code_ref(facts, detail or "no pooling"),
        note=note,
    )


def _check_output_layer(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    text = _evidence_text(card)
    numbers = [
        int(m)
        for m in re.findall(r"(?:output|classes|units|digits|ways)(?:\s+[a-z]+){0,2}\s+(\d+)", text)
    ]
    numbers += [
        int(m) for m in re.findall(r"(\d+)\s*(?:output|classes|units|ways|digits)", text)
    ]
    numbers = sorted(set(numbers))
    impl = facts.output_features
    if impl is None:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取输出层维度"
    elif not numbers:
        status, note = CheckStatus.UNKNOWN, "原文证据未明确输出维度，无法比对"
    elif impl in numbers:
        status, note = CheckStatus.MATCHED, f"原文输出维度证据 {numbers} 与实现 {impl} 一致"
    else:
        status, note = (
            CheckStatus.DEVIATED,
            f"原文输出维度证据 {numbers} 与实现 {impl} 不一致",
        )
    return MethodCheck(
        item=MethodItem.OUTPUT_LAYER,
        status=status,
        paper_value=", ".join(str(n) for n in numbers) if numbers else None,
        implementation_value=str(impl) if impl is not None else None,
        paper_evidence=_paper_ref(card.model_architecture) or _paper_ref(card.method),
        implementation_evidence=_code_ref(facts, f"output_features={impl}"),
        note=note,
    )


def _check_loss(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    field = card.loss_function
    impl = facts.loss_function
    paper_raw = str(field.value).strip().lower() if _paper_ref(field) else None
    paper = _LOSS_ALIASES.get(paper_raw, paper_raw) if paper_raw else None
    if impl is None:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取损失函数"
    elif field.status is EvidenceStatus.MISSING or paper is None:
        status, note = CheckStatus.UNKNOWN, "Card 未提供损失函数的原文证据"
    elif paper == impl:
        status, note = CheckStatus.MATCHED, f"原文损失 {paper_raw} 与实现 {impl} 一致"
    else:
        status, note = (
            CheckStatus.DEVIATED,
            f"原文损失 {paper_raw}（归一化 {paper}）与实现 {impl} 不一致",
        )
    return MethodCheck(
        item=MethodItem.LOSS_FUNCTION,
        status=status,
        paper_value=paper_raw,
        implementation_value=impl,
        paper_evidence=_paper_ref(field),
        implementation_evidence=_code_ref(facts, f"loss={impl}"),
        note=note,
    )


def _check_preprocessing(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    impl_steps = set(facts.preprocessing)
    detail = ", ".join(facts.preprocessing) or "none"
    if facts.normalize_mean is not None:
        detail += f" (mean={facts.normalize_mean}, std={facts.normalize_std})"
    entries = {
        key: field
        for key, field in card.data_preprocessing.items()
        if _paper_ref(field) is not None
    }
    if not facts.preprocessing:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取预处理事实"
    elif not entries:
        status, note = CheckStatus.UNKNOWN, "Card 未提供预处理原文证据；实现采用 MVP 默认预处理"
    else:
        deviations: list[str] = []
        matched: list[str] = []
        for key, field in entries.items():
            text = f"{key} {field.value}".lower()
            if re.search(r"normaliz", text):
                (matched if "normalize" in impl_steps else deviations).append(key)
            elif re.search(r"tensor", text):
                (matched if "to_tensor" in impl_steps else deviations).append(key)
            elif re.search(r"resiz|crop|flip|augment|whiten|pca|pad", text):
                deviations.append(key)
            else:
                deviations.append(f"{key} (无法映射到实现步骤)")
        if deviations:
            status = CheckStatus.DEVIATED
            note = f"预处理与实现不一致：{', '.join(deviations)}"
        elif matched:
            status = CheckStatus.MATCHED
            note = f"原文预处理步骤 {', '.join(matched)} 与实现一致"
        else:
            status = CheckStatus.UNKNOWN
            note = "原文预处理证据无法确定性映射到实现步骤"
    first_ref = next((_paper_ref(f) for f in entries.values()), None) if entries else None
    return MethodCheck(
        item=MethodItem.PREPROCESSING,
        status=status,
        paper_value="; ".join(f"{k}={f.value}" for k, f in entries.items()) if entries else None,
        implementation_value=detail if facts.preprocessing else None,
        paper_evidence=first_ref,
        implementation_evidence=_code_ref(facts, detail),
        note=note,
    )


def _check_dataset(card: ReproductionCard, facts: ImplementationFacts) -> MethodCheck:
    field = card.dataset
    paper = str(field.value).lower() if _paper_ref(field) else ""
    impl = facts.dataset
    if impl is None:
        status, note = CheckStatus.UNKNOWN, "无法从已固定代码提取数据集事实"
    elif field.status is EvidenceStatus.MISSING or not paper:
        status, note = CheckStatus.UNKNOWN, "Card 未提供数据集的原文证据"
    elif "mnist" in paper and impl == "mnist":
        status, note = CheckStatus.MATCHED, "原文数据集 MNIST 与实现一致"
    elif "mnist" not in paper:
        status, note = CheckStatus.DEVIATED, f"原文数据集「{field.value}」与实现 {impl} 不一致"
    else:
        status, note = CheckStatus.DEVIATED, f"原文 MNIST 与实现 {impl} 不一致"
    return MethodCheck(
        item=MethodItem.DATASET,
        status=status,
        paper_value=str(field.value) if _paper_ref(field) else None,
        implementation_value=impl,
        paper_evidence=_paper_ref(field),
        implementation_evidence=_code_ref(facts, f"dataset={impl}"),
        note=note,
    )


def validate_method(card: ReproductionCard, facts: ImplementationFacts) -> list[MethodCheck]:
    return [
        _check_architecture(card, facts),
        _check_layers(card, facts),
        _check_activation(card, facts),
        _check_subsampling(card, facts),
        _check_output_layer(card, facts),
        _check_loss(card, facts),
        _check_preprocessing(card, facts),
        _check_dataset(card, facts),
    ]