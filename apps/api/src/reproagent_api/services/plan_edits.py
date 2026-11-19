"""Apply user/OOM edits to a plan, producing a new version's content + diff.

Edits never mutate the base version: the caller inserts the result as a new row.
Confirming a parameter clears its requires_confirmation flag and syncs the
matching assumption, which is what unlocks the approval gate.
"""

from reproagent_schemas import (
    ExperimentPlan,
    ParamSource,
    PlanChangeRecord,
    PlanEditRequest,
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
}


class PlanEditError(ValueError):
    pass


def apply_plan_edits(
    plan: ExperimentPlan, request: PlanEditRequest
) -> tuple[ExperimentPlan, list[PlanChangeRecord]]:
    data = plan.model_dump(mode="json")
    records: list[PlanChangeRecord] = []
    for change in request.changes:
        fp = change.field_path
        parts = fp.split(".")
        if fp in PARAM_PATHS:
            node = data[parts[0]].get(parts[1])
            before = node["value"] if node else None
            if node is None:
                node = {}
                data[parts[0]][parts[1]] = node
            node["value"] = change.value
            node["human_confirmed"] = change.human_confirmed
            if change.human_confirmed:
                node["source"] = ParamSource.HUMAN_CONFIRMED.value
                node["requires_confirmation"] = False
            for assumption in data["assumptions"]:
                if assumption["field_path"] == fp:
                    assumption["value"] = change.value
                    assumption["human_confirmed"] = change.human_confirmed
                    if change.human_confirmed:
                        assumption["requires_confirmation"] = False
            records.append(PlanChangeRecord(field_path=fp, before=before, after=change.value))
        elif fp in SCALAR_PATHS:
            container = data[parts[0]]
            before = container[parts[1]]
            container[parts[1]] = change.value
            records.append(PlanChangeRecord(field_path=fp, before=before, after=change.value))
        elif fp.startswith("preprocessing_config."):
            name = fp.split(".", 1)[1]
            step = next(
                (s for s in data["preprocessing_config"]["steps"] if s["name"] == name), None
            )
            if step is None:
                raise PlanEditError(f"unknown preprocessing step '{name}'")
            before = step["detail"]
            step["detail"] = str(change.value)
            if change.human_confirmed:
                step["source"] = ParamSource.HUMAN_CONFIRMED.value
            for assumption in data["assumptions"]:
                if assumption["field_path"] == fp:
                    assumption["value"] = change.value
                    assumption["human_confirmed"] = change.human_confirmed
                    if change.human_confirmed:
                        assumption["requires_confirmation"] = False
            records.append(PlanChangeRecord(field_path=fp, before=before, after=change.value))
        else:
            raise PlanEditError(f"field '{fp}' is not editable")
    try:
        new_plan = ExperimentPlan.model_validate(data)
    except ValueError as exc:
        raise PlanEditError(f"edited plan is invalid: {exc}") from exc
    return new_plan, records
