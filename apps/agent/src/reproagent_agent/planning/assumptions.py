"""Deterministic provenance helpers: card evidence -> plan params + assumptions.

A parameter the paper does not state is NEVER silently defaulted. It becomes a
flagged proposal (requires_confirmation=True) recorded in the plan's assumptions,
so the approval gate can block until a human confirms it.
"""

import re

from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    EvidenceField,
    EvidenceStatus,
    ParamSource,
    PlanParam,
    RiskTier,
)

from .scaling import SUPPORTED_OPTIMIZERS

_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

_OPTIMIZER_ALIASES = {
    "sgd": "sgd",
    "stochastic gradient descent": "sgd",
    "adam": "adam",
    "adamw": "adamw",
    "adagrad": "adagrad",
    "adadelta": "adadelta",
    "rmsprop": "rmsprop",
}


def as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER.search(value)
        if match:
            return float(match.group())
    return None


def as_int(value: object) -> int | None:
    number = as_float(value)
    return int(number) if number is not None else None


def normalize_optimizer(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in _OPTIMIZER_ALIASES:
        return _OPTIMIZER_ALIASES[key]
    if key in SUPPORTED_OPTIMIZERS:
        return key
    return None


# The static train template implements cross-entropy only. These aliases mirror
# the cross_entropy family in evaluation.method._LOSS_ALIASES; any other loss
# (nll/mse/bce/prose) is unsupported and must fail at generation, never silently
# run as cross-entropy.
_LOSS_ALIASES = {
    "cross entropy": "cross_entropy",
    "cross-entropy": "cross_entropy",
    "cross_entropy": "cross_entropy",
    "softmax loss": "cross_entropy",
    "softmax cross entropy": "cross_entropy",
}


def normalize_loss(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return _LOSS_ALIASES.get(raw.strip().lower())


def param_from_evidence(
    field: EvidenceField,
    field_path: str,
    *,
    fallback_value: object,
    fallback_rationale: str,
    cast,
    assumptions: list[Assumption],
    risk_tier: RiskTier = RiskTier.CRITICAL,
    auto_acceptable: bool = False,
) -> PlanParam:
    """Map a card evidence field to an executable plan param with provenance."""
    quote = field.evidence_quote
    page = field.source_page

    if (
        field.status in (EvidenceStatus.PAPER_EXPLICIT, EvidenceStatus.PAPER_DESCRIBED)
        and field.value is not None
    ):
        value = cast(field.value)
        if value is not None:
            return PlanParam(
                value=value,
                source=ParamSource.PAPER_EXPLICIT,
                paper_value=value,
                evidence_quote=quote,
                source_page=page,
                human_confirmed=field.human_confirmed,
            )

    if field.status is EvidenceStatus.INFERRED and field.value is not None:
        value = cast(field.value)
        if value is not None:
            confirmed = field.human_confirmed
            rationale = f"理解阶段推断（置信度 {field.confidence:.0%}），论文未直接说明"
            assumptions.append(
                Assumption(
                    field_path=field_path,
                    kind=AssumptionKind.INFERRED,
                    value=value,
                    rationale=rationale,
                    requires_confirmation=not confirmed,
                    human_confirmed=confirmed,
                    risk_tier=risk_tier,
                    auto_acceptable=auto_acceptable,
                    card_status=EvidenceStatus.INFERRED,
                    evidence_quote=quote,
                    source_page=page,
                )
            )
            return PlanParam(
                value=value,
                source=ParamSource.HUMAN_CONFIRMED if confirmed else ParamSource.INFERRED,
                paper_value=value,
                evidence_quote=quote,
                source_page=page,
                rationale=rationale,
                requires_confirmation=not confirmed,
                human_confirmed=confirmed,
            )

    # missing (or unparseable) -> flagged proposal, never a silent default
    assumptions.append(
        Assumption(
            field_path=field_path,
            kind=AssumptionKind.MISSING_PROPOSAL,
            value=fallback_value,
            rationale=fallback_rationale,
            requires_confirmation=True,
            human_confirmed=False,
            risk_tier=risk_tier,
            auto_acceptable=auto_acceptable,
            card_status=field.status,
        )
    )
    return PlanParam(
        value=fallback_value,
        source=ParamSource.INFERRED,
        rationale=fallback_rationale,
        requires_confirmation=True,
        human_confirmed=False,
    )
