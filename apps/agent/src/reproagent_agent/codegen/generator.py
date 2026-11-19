"""Deterministic code generation from an approved ExperimentPlan.

No LLM is involved: the executable code is a fixed, audited template set and the
plan only drives config.yaml. The resulting CodeBundle carries per-file sha256
and an aggregate code_hash so every run can pin an exact, reproducible version.
"""

from reproagent_schemas import (
    DEPENDENCY_WHITELIST,
    CodeBundle,
    CodeFile,
    ExperimentPlan,
    ReproductionCard,
)

from .templates import (
    EVALUATE_PY,
    MODEL_PY,
    TRAIN_PY,
    render_config_yaml,
    requirements_txt,
)


def generate_from_plan(plan: ExperimentPlan) -> CodeBundle:
    files = [
        CodeFile(path="model.py", content=MODEL_PY),
        CodeFile(path="train.py", content=TRAIN_PY),
        CodeFile(path="evaluate.py", content=EVALUATE_PY),
        CodeFile(path="config.yaml", content=render_config_yaml(plan)),
        CodeFile(path="requirements.txt", content=requirements_txt()),
    ]
    return CodeBundle(
        files=files,
        entrypoint="train.py",
        requirements=[f"{name}=={version}" for name, version in DEPENDENCY_WHITELIST.items()],
    )


def generate_code(card: ReproductionCard, plan: ExperimentPlan) -> CodeBundle:
    return generate_from_plan(plan)