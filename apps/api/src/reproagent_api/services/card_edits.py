"""Apply review decisions without mutating the base card version."""

from uuid import UUID

from reproagent_schemas import (
    CardEditRequest,
    ConfirmationSource,
    EvidenceStatus,
    ReproductionCard,
    ReviewDecision,
    ReviewMode,
    utcnow,
)

SCALAR_FIELDS = ("method", "model_architecture", "dataset", "optimizer", "loss_function")
DICT_FIELDS = ("data_preprocessing", "hyperparameters")
LIST_FIELDS = ("evaluation_metrics", "reported_results", "baselines")


class EditError(ValueError):
    pass


def apply_edits(
    card: ReproductionCard, request: CardEditRequest,
    *, confirmed_by: ConfirmationSource = ConfirmationSource.HUMAN,
    actor: UUID | None = None,
) -> ReproductionCard:
    if confirmed_by not in (ConfirmationSource.HUMAN, ConfirmationSource.AI_ASSISTED):
        raise EditError("automatic acceptance requires delegated review")
    data = card.model_dump(mode="json")
    now = utcnow()
    mode = request.review_mode or (
        ReviewMode.ASSISTED if card.review_mode is ReviewMode.DELEGATED else card.review_mode
    )
    data.update(review_mode=mode, delegated_at=None, delegated_by=None)
    if mode != card.review_mode:
        data["review_decisions"].append(ReviewDecision(
            field_path="review_mode", before=card.review_mode, after=mode,
            review_mode=mode, actor=actor, timestamp=now,
        ).model_dump(mode="json"))
    if len({e.field_path for e in request.edits}) != len(request.edits):
        raise EditError("duplicate field edits")
    for edit in request.edits:
        parts = edit.field_path.split(".")
        if len(parts) == 1:
            if parts[0] not in SCALAR_FIELDS:
                raise EditError(f"field '{edit.field_path}' is not editable")
            field = data[parts[0]]
        else:
            container, key = parts
            if container not in DICT_FIELDS:
                raise EditError(f"field '{edit.field_path}' is not editable")
            if key not in data[container]:
                raise EditError(f"unknown entry '{edit.field_path}'")
            field = data[container][key]

        before = field["value"]
        if confirmed_by is ConfirmationSource.AI_ASSISTED and (
            field["status"] != EvidenceStatus.INFERRED.value
            or field["value"] != edit.value or not edit.human_confirmed
            or isinstance(field["value"], bool) != isinstance(edit.value, bool)
        ):
            raise EditError("AI-assisted card confirmation must preserve an inferred value")
        field["confirmed_by"] = confirmed_by if edit.human_confirmed else None
        if edit.value is None:
            field.update(
                value=None,
                status=EvidenceStatus.MISSING.value,
                evidence_quote=None,
                source_page=None,
                source_section=None,
                human_confirmed=edit.human_confirmed,
            )
        else:
            field["value"] = edit.value
            if field["status"] == EvidenceStatus.MISSING.value:
                field["status"] = EvidenceStatus.INFERRED.value
            field["human_confirmed"] = edit.human_confirmed
        data["review_decisions"].append(ReviewDecision(
            field_path=edit.field_path, before=before, after=edit.value,
            confirmed_by=field["confirmed_by"], review_mode=mode, actor=actor, timestamp=now,
        ).model_dump(mode="json"))
    try:
        return ReproductionCard.model_validate(data)
    except ValueError as exc:
        raise EditError(f"edited card is invalid: {exc}") from exc


def delegate_card(card: ReproductionCard, *, actor: UUID) -> ReproductionCard:
    """Record explicit consent and acknowledge inferences/missingness, not evidence."""
    if not isinstance(actor, UUID):
        raise EditError("delegated review requires an actor")
    if card.delegated_by is not None and card.delegated_by != actor:
        raise EditError("delegated authorization belongs to another actor")
    data = card.model_dump(mode="json")
    now = utcnow()
    data.update(review_mode=ReviewMode.DELEGATED, delegated_at=now, delegated_by=actor)
    decisions = [ReviewDecision(
        field_path="review_mode", before=card.review_mode, after=ReviewMode.DELEGATED,
        review_mode=ReviewMode.DELEGATED, actor=actor, timestamp=now,
    )]
    fields = [(name, data[name]) for name in SCALAR_FIELDS]
    for name in DICT_FIELDS:
        fields.extend((f"{name}.{key}", field) for key, field in data[name].items())
    for name in LIST_FIELDS:
        fields.extend((f"{name}.{index}", field) for index, field in enumerate(data[name]))
    for path, field in fields:
        if field["human_confirmed"] or field["status"] not in (
            EvidenceStatus.INFERRED, EvidenceStatus.MISSING,
        ):
            continue
        field.update(confirmed_by=ConfirmationSource.AUTO, human_confirmed=False)
        decisions.append(ReviewDecision(
            field_path=path, before=field["value"], after=field["value"],
            confirmed_by=ConfirmationSource.AUTO, review_mode=ReviewMode.DELEGATED,
            actor=actor, timestamp=now,
        ))
    data["review_decisions"].extend(d.model_dump(mode="json") for d in decisions)
    try:
        return ReproductionCard.model_validate(data)
    except ValueError as exc:
        raise EditError(f"delegated card is invalid: {exc}") from exc
