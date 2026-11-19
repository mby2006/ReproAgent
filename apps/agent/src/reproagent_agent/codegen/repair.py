"""Deterministic error repair.

Code-fixable categories are repaired by re-rendering the canonical, audited
template set from the approved plan -- this produces a new, correct code version
(new code_hash) without an LLM. OUT_OF_MEMORY is NOT repaired by silently editing
training targets: it flags replan_required so the Worker asks the Planner for a
new ExperimentPlan version (e.g. reduced batch size). UNKNOWN errors are left
unchanged for human review.
"""

from reproagent_schemas import ErrorCategory, RepairRequest, RepairResponse

from .generator import generate_from_plan

_CODE_FIXABLE = {
    ErrorCategory.SYNTAX_ERROR,
    ErrorCategory.IMPORT_ERROR,
    ErrorCategory.SHAPE_MISMATCH,
    ErrorCategory.DATA_DOWNLOAD_ERROR,
    ErrorCategory.METRIC_ERROR,
}

_REASONS = {
    ErrorCategory.SYNTAX_ERROR: "re-rendered canonical templates to eliminate syntax error",
    ErrorCategory.IMPORT_ERROR: "re-rendered templates with pinned whitelist imports",
    ErrorCategory.SHAPE_MISMATCH: "re-rendered templates with plan-consistent input shape",
    ErrorCategory.DATA_DOWNLOAD_ERROR: "re-rendered templates using baked offline dataset",
    ErrorCategory.METRIC_ERROR: "re-rendered templates with the deterministic metrics interface",
}


def repair_bundle(request: RepairRequest) -> RepairResponse:
    category = request.classification.category
    if category is ErrorCategory.OUT_OF_MEMORY:
        return RepairResponse(
            bundle=request.bundle,
            change_reason=(
                "OUT_OF_MEMORY: training targets are Planner-owned; requesting a new "
                "ExperimentPlan version with a reduced batch size"
            ),
            replan_required=True,
        )
    if category in _CODE_FIXABLE:
        return RepairResponse(
            bundle=generate_from_plan(request.plan),
            change_reason=f"{category.value}: {_REASONS[category]}",
            replan_required=False,
        )
    return RepairResponse(
        bundle=request.bundle,
        change_reason=f"{category.value}: no deterministic repair available; needs human review",
        replan_required=False,
    )