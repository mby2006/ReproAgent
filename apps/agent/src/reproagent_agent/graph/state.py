from typing import TypedDict

from reproagent_schemas import (
    CodeBundle,
    EvaluationReport,
    ExperimentPlan,
    ParsedDocument,
    ReproductionCard,
    ReproductionSpec,
    RunResult,
)

from ..planning.scaling import ScalingConfig


class AgentState(TypedDict, total=False):
    job_id: str
    user_id: str
    paper_title: str
    paper_text: str | None
    pdf_b64: str | None
    document: ParsedDocument | None
    card: ReproductionCard | None
    scaling: ScalingConfig | None
    spec: ReproductionSpec | None
    plan: ExperimentPlan | None
    code_bundle: CodeBundle | None
    execution_result: RunResult | None
    report: EvaluationReport | None
    errors: list[str]
