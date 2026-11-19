"""Apply user edits to a card, producing a new version's content.

Edits never mutate the base version: the caller inserts the result as a new row.
"""

from reproagent_schemas import CardEditRequest, EvidenceStatus, ReproductionCard

SCALAR_FIELDS = ("method", "model_architecture", "dataset", "optimizer", "loss_function")
DICT_FIELDS = ("data_preprocessing", "hyperparameters")


class EditError(ValueError):
    pass


def apply_edits(card: ReproductionCard, request: CardEditRequest) -> ReproductionCard:
    data = card.model_dump(mode="json")
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
    try:
        return ReproductionCard.model_validate(data)
    except ValueError as exc:
        raise EditError(f"edited card is invalid: {exc}") from exc
