"""Apply review decisions without mutating the base card version."""

from uuid import UUID

from reproagent_schemas import (
    CardEditRequest,
    ConfirmationSource,
    EvidenceStatus,
    GroundingStatus,
    ReproductionCard,
    ReviewDecision,
    ReviewMode,
    utcnow,
)

SCALAR_FIELDS = ("method", "model_architecture", "dataset", "optimizer", "loss_function")
DICT_FIELDS = ("data_preprocessing", "hyperparameters")
LIST_FIELDS = ("evaluation_metrics", "reported_results", "baselines")

_VALUE_CHANGED_REASON = "字段值已改变，原引用不能作为新值的论文证据"
_NO_REVOCABLE_REASON = "旧字段没有可撤销的已接受引用"


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

        old_evidence = {
            "status": field["status"],
            "grounding_status": field.get("grounding_status", GroundingStatus.NOT_CHECKED.value),
            "value": field.get("value"),
            "evidence_quote": field.get("evidence_quote"),
            "source_page": field.get("source_page"),
            "source_section": field.get("source_section"),
        }
        value_changed = edit.value != before
        grounding_reason = None
        previous_evidence = None

        if not value_changed:
            # Same value: never revoke the accepted citation or its grounding. Only the
            # human-confirmation provenance is (re)recorded.
            field["human_confirmed"] = edit.human_confirmed
        else:
            had_accepted_citation = bool(old_evidence["evidence_quote"])
            if edit.value is None:
                # Field cleared -> MISSING carries no value, citation or candidate.
                field.update(
                    value=None,
                    status=EvidenceStatus.MISSING.value,
                    grounding_status=GroundingStatus.NOT_CHECKED.value,
                    evidence_quote=None,
                    source_page=None,
                    source_section=None,
                    candidate_quote=None,
                    candidate_page=None,
                    candidate_section=None,
                    grounding_reason=None,
                    human_confirmed=edit.human_confirmed,
                )
            elif had_accepted_citation:
                # Demote the stale accepted citation to a rejected candidate. The new
                # value is unverified: locating the old quote never proved support.
                field.update(
                    value=edit.value,
                    status=EvidenceStatus.INFERRED.value,
                    grounding_status=GroundingStatus.LOCATED_UNSUPPORTED.value,
                    evidence_quote=None,
                    source_page=None,
                    source_section=None,
                    candidate_quote=old_evidence["evidence_quote"],
                    candidate_page=old_evidence["source_page"],
                    candidate_section=old_evidence["source_section"],
                    grounding_reason=_VALUE_CHANGED_REASON,
                    human_confirmed=edit.human_confirmed,
                )
            else:
                # No accepted citation to revoke. Keep any prior rejected candidate as
                # review context; a fresh inferred value stays unchecked (never VERIFIED).
                field.update(
                    value=edit.value,
                    status=EvidenceStatus.INFERRED.value,
                    grounding_status=GroundingStatus.NOT_CHECKED.value,
                    evidence_quote=None,
                    source_page=None,
                    source_section=None,
                    candidate_quote=field.get("candidate_quote"),
                    candidate_page=field.get("candidate_page"),
                    candidate_section=field.get("candidate_section"),
                    grounding_reason=None,
                    human_confirmed=edit.human_confirmed,
                )
            previous_evidence = old_evidence
            grounding_reason = (
                _VALUE_CHANGED_REASON
                if edit.value is not None and had_accepted_citation
                else _NO_REVOCABLE_REASON
                if edit.value is not None
                else None
            )
        data["review_decisions"].append(ReviewDecision(
            field_path=edit.field_path, before=before, after=edit.value,
            confirmed_by=field["confirmed_by"], review_mode=mode, actor=actor, timestamp=now,
            grounding_reason=grounding_reason, previous_evidence=previous_evidence,
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
