import math
from datetime import datetime
from uuid import UUID

from reproagent_schemas import (
    ConfirmationSource,
    ExperimentPlan,
    FieldRecommendation,
    ParamSource,
    PlanChange,
    PlanChangeRecord,
    PlanEditRequest,
    ReviewMode,
    RiskTier,
    utcnow,
)

PARAM_PATHS = {
    "training_config.optimizer",
    "training_config.learning_rate",
    "training_config.loss_function",
    "training_config.weight_decay",
    "training_config.batch_size",
    "training_config.epochs",
}
SCALAR_PATHS = {
    "dataset_config.train_subset_size",
    "dataset_config.test_subset_size",
    "runtime_budget.max_seconds",
    "evaluation_config.tolerance",
    "evaluation_config.target_metric",
}


class PlanEditError(ValueError):
    pass


def _same_value(left: object, right: object) -> bool:
    # Numeric equivalence is fine; Python's True == 1 is not.
    same_type = type(left) is type(right) or (
        type(left) in (int, float) and type(right) in (int, float)
    )
    return same_type and left == right


def _editable_node(data: dict, path: str) -> tuple[dict, str] | None:
    if path in PARAM_PATHS:
        container, name = path.split(".")
        node = data[container].get(name)
        return (node, "value") if node is not None else None
    if path in SCALAR_PATHS:
        container, name = path.split(".")
        return data[container], name
    if path.startswith("preprocessing_config."):
        name = path.split(".", 1)[1]
        step = next((s for s in data["preprocessing_config"]["steps"] if s["name"] == name), None)
        return (step, "detail") if step is not None else None
    return None


def _apply_changes(
    data: dict,
    changes: list[PlanChange],
    *,
    confirmed_by: ConfirmationSource,
    actor: UUID | None,
    recommendations: dict[str, FieldRecommendation] | None,
    preselection_threshold: float | None = None,
    timestamp: datetime,
) -> tuple[ExperimentPlan, list[PlanChangeRecord]]:
    records: list[PlanChangeRecord] = []
    if len({c.field_path for c in changes}) != len(changes):
        raise PlanEditError("duplicate field changes")
    auto = confirmed_by is ConfirmationSource.AUTO
    assisted = confirmed_by is ConfirmationSource.AI_ASSISTED
    for change in changes:
        fp = change.field_path
        assumption = next((a for a in data["assumptions"] if a["field_path"] == fp), None)
        recommendation = (recommendations or {}).get(fp)
        editable = _editable_node(data, fp)
        if auto or assisted:
            if (
                assumption is None or not assumption["requires_confirmation"]
                or assumption["human_confirmed"] or assumption["risk_tier"] == RiskTier.INFO
                or recommendation is None or recommendation.field_path != fp
                or not _same_value(recommendation.suggested_value, change.value)
                or change.human_confirmed == auto
            ):
                raise PlanEditError("AI adoption requires a pending, validated recommendation")
            if recommendation.action == "accept" and not _same_value(
                change.value, assumption["value"]
            ):
                raise PlanEditError("accept must preserve the current value")
            if (
                (fp in PARAM_PATHS | SCALAR_PATHS and editable is None)
                or (editable is not None and not _same_value(
                    editable[0][editable[1]], assumption["value"],
                ))
            ):
                raise PlanEditError("assumption does not match the executable plan value")
            if isinstance(change.value, float) and not math.isfinite(change.value):
                raise PlanEditError("recommendation must contain a finite value")
            if auto:
                if recommendation.action == "modify" and fp not in PARAM_PATHS | SCALAR_PATHS:
                    raise PlanEditError("delegation cannot rewrite a read-only assumption")
                if fp in {"training_config.learning_rate", "training_config.weight_decay"}:
                    value = change.value
                    if type(value) not in (int, float) or (
                        value <= 0 if fp.endswith("learning_rate") else value < 0
                    ):
                        raise PlanEditError("automatic training parameter outside safe bounds")
                if fp == "runtime_budget.max_seconds" and (
                    type(change.value) is not int or change.value <= 0
                    or change.value > data["runtime_budget"]["max_seconds"]
                ):
                    raise PlanEditError("delegation cannot increase the authorized runtime budget")
        provenance = confirmed_by if auto or change.human_confirmed else None
        if fp in PARAM_PATHS:
            container, name = fp.split(".")
            node = data[container].get(name)
            before = node["value"] if node else None
            if node is None:
                node = {}
                data[container][name] = node
            node["value"] = change.value
            node["human_confirmed"] = change.human_confirmed
            node["confirmed_by"] = provenance
            if auto or change.human_confirmed:
                node["source"] = (
                    ParamSource.INFERRED.value if auto or assisted
                    else ParamSource.HUMAN_CONFIRMED.value
                )
                node["requires_confirmation"] = auto
            elif assumption is not None:
                node["requires_confirmation"] = True
            if auto or assisted:
                node["rationale"] = recommendation.reasoning
        elif editable is not None:
            node, key = editable
            before = node[key]
            node[key] = change.value
            if fp == "evaluation_config.target_metric" and change.value != before:
                # These numbers describe the old target, not the newly selected metric.
                node.update(reported_value=None, reported_condition=None, reported_source_page=None)
            if fp.startswith("preprocessing_config."):
                node[key] = str(change.value)
                if auto or change.human_confirmed:
                    node["source"] = (
                        ParamSource.INFERRED.value if auto or assisted
                        else ParamSource.HUMAN_CONFIRMED.value
                    )
                if auto or assisted:
                    node["rationale"] = recommendation.reasoning
        elif auto and recommendation.action == "accept":
            # Acknowledging a read-only assumption must not mutate executable configuration.
            before = assumption["value"]
        else:
            raise PlanEditError(f"field '{fp}' is not editable")
        if assumption is not None:
            assumption["value"] = change.value
            assumption["human_confirmed"] = change.human_confirmed
            assumption["confirmed_by"] = provenance
            assumption["requires_confirmation"] = not change.human_confirmed
        records.append(PlanChangeRecord(
            field_path=fp, before=before, after=change.value,
            confirmed_by=provenance, review_mode=data["review_mode"], actor=actor,
            timestamp=timestamp,
            recommendation_id=recommendation.id if recommendation else None,
            recommendation_confidence=recommendation.confidence if recommendation else None,
            recommendation_reasoning=recommendation.reasoning if recommendation else None,
            recommendation_risk=recommendation.risk if recommendation else None,
            preselection_threshold=preselection_threshold,
        ))
    try:
        new_plan = ExperimentPlan.model_validate(data)
    except ValueError as exc:
        raise PlanEditError(f"edited plan is invalid: {exc}") from exc
    if auto or assisted:
        validated = new_plan.model_dump(mode="json")
        for change in changes:
            editable = _editable_node(validated, change.field_path)
            if editable is not None and not _same_value(
                editable[0][editable[1]], change.value
            ):
                raise PlanEditError("recommendation value must not require type coercion")
    return new_plan, records


def apply_plan_edits(
    plan: ExperimentPlan,
    request: PlanEditRequest,
    *,
    confirmed_by: ConfirmationSource = ConfirmationSource.HUMAN,
    actor: UUID | None = None,
    recommendations: dict[str, FieldRecommendation] | None = None,
    preselection_threshold: float | None = None,
) -> tuple[ExperimentPlan, list[PlanChangeRecord]]:
    if confirmed_by not in (ConfirmationSource.HUMAN, ConfirmationSource.AI_ASSISTED):
        raise PlanEditError("automatic acceptance requires delegated review")
    data = plan.model_dump(mode="json")
    mode = request.review_mode or (
        ReviewMode.ASSISTED if plan.review_mode is ReviewMode.DELEGATED else plan.review_mode
    )
    data.update(review_mode=mode, delegated_at=None, delegated_by=None)
    now = utcnow()
    new_plan, records = _apply_changes(
        data, request.changes, confirmed_by=confirmed_by, actor=actor,
        recommendations=recommendations, preselection_threshold=preselection_threshold,
        timestamp=now,
    )
    if mode != plan.review_mode:
        records.insert(0, PlanChangeRecord(
            field_path="review_mode", before=plan.review_mode, after=mode,
            review_mode=mode, actor=actor, timestamp=now,
        ))
    return new_plan, records


def apply_delegated_review(
    plan: ExperimentPlan,
    recommendations: list[FieldRecommendation],
    *,
    actor: UUID,
    delegated_at: datetime | None = None,
) -> tuple[ExperimentPlan, list[PlanChangeRecord]]:
    """Accept an exact, complete recommendation set under explicit API consent."""
    if not isinstance(actor, UUID):
        raise PlanEditError("delegated review requires an actor")
    if plan.delegated_by is not None and plan.delegated_by != actor:
        raise PlanEditError("delegated authorization belongs to another actor")
    pending = [
        a for a in plan.unconfirmed_required() if a.risk_tier is not RiskTier.INFO
    ]
    paths = {a.field_path for a in pending}
    advice = {r.field_path: r for r in recommendations}
    if len(paths) != len(pending):
        raise PlanEditError("duplicate pending assumptions")
    if (
        set(advice) != paths or len(advice) != len(recommendations)
        or len({r.id for r in recommendations}) != len(recommendations)
    ):
        raise PlanEditError("delegated review requires the complete, distinct pending set")
    now = utcnow()
    authorized_at = delegated_at if delegated_at is not None else now
    data = plan.model_dump(mode="json")
    data.update(review_mode=ReviewMode.DELEGATED, delegated_at=authorized_at, delegated_by=actor)
    new_plan, records = _apply_changes(
        data,
        [PlanChange(field_path=r.field_path, value=r.suggested_value, human_confirmed=False)
         for r in recommendations],
        confirmed_by=ConfirmationSource.AUTO, actor=actor, recommendations=advice,
        timestamp=now,
    )
    records.insert(0, PlanChangeRecord(
        field_path="review_mode", before=plan.review_mode, after=ReviewMode.DELEGATED,
        review_mode=ReviewMode.DELEGATED, actor=actor, timestamp=now,
    ))
    return new_plan, records


def apply_tier_override(
    plan: ExperimentPlan,
    field_path: str,
    risk_tier: RiskTier,
    *,
    actor: UUID,
) -> tuple[ExperimentPlan, list[PlanChangeRecord]]:
    """Expert re-tiering is a human decision: new version, audited, never silent."""
    data = plan.model_dump(mode="json")
    assumption = next(
        (a for a in data["assumptions"] if a["field_path"] == field_path), None
    )
    if assumption is None:
        raise PlanEditError(f"assumption '{field_path}' not found")
    before = assumption["risk_tier"]
    if before == risk_tier.value:
        raise PlanEditError("risk tier is unchanged")
    mode = (
        ReviewMode.ASSISTED if plan.review_mode is ReviewMode.DELEGATED else plan.review_mode
    )
    data.update(review_mode=mode, delegated_at=None, delegated_by=None)
    assumption["risk_tier"] = risk_tier.value
    assumption["auto_acceptable"] = risk_tier is not RiskTier.CRITICAL
    if risk_tier is RiskTier.INFO:
        assumption["requires_confirmation"] = False
    elif not assumption["human_confirmed"]:
        assumption["requires_confirmation"] = True
    if field_path in PARAM_PATHS:
        container, name = field_path.split(".")
        node = data[container].get(name)
        if node is not None:
            node["requires_confirmation"] = assumption["requires_confirmation"]
    now = utcnow()
    records = [PlanChangeRecord(
        field_path=f"{field_path}.risk_tier", before=before, after=risk_tier.value,
        confirmed_by=ConfirmationSource.HUMAN, review_mode=mode, actor=actor, timestamp=now,
    )]
    if mode != plan.review_mode:
        records.insert(0, PlanChangeRecord(
            field_path="review_mode", before=plan.review_mode, after=mode,
            review_mode=mode, actor=actor, timestamp=now,
        ))
    try:
        new_plan = ExperimentPlan.model_validate(data)
    except ValueError as exc:
        raise PlanEditError(f"edited plan is invalid: {exc}") from exc
    return new_plan, records
