"""Code generation contracts: approved plan -> validated, versioned code bundle.

Generation is deterministic (template-based for LeNet-5/MNIST v1). Every bundle
carries a content hash so runs pin exact code; static validation and error
classification/repair are pure, auditable steps driven by the worker.
"""

import hashlib

from pydantic import Field, model_validator

from .card import ReproductionCard
from .common import SchemaModel
from .execution import CodeFile, ErrorCategory, RunStatus, is_whitelisted
from .plan import ExperimentPlan


def compute_code_hash(files: list[CodeFile]) -> str:
    joined = "\n".join(f"{f.path}:{f.sha256}" for f in sorted(files, key=lambda x: x.path))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class CodeBundle(SchemaModel):
    files: list[CodeFile] = Field(min_length=1)
    entrypoint: str = Field(default="train.py", min_length=1)
    requirements: list[str] = Field(default_factory=list)
    code_hash: str = ""
    metrics_schema_version: str = "1.0"

    @model_validator(mode="after")
    def check_bundle(self) -> "CodeBundle":
        for requirement in self.requirements:
            if not is_whitelisted(requirement):
                raise ValueError(f"dependency not in pinned whitelist: {requirement}")
        paths = {f.path for f in self.files}
        if self.entrypoint not in paths:
            raise ValueError(f"entrypoint '{self.entrypoint}' not present in bundle")
        if not self.code_hash:
            self.code_hash = compute_code_hash(self.files)
        return self


class CodegenRequest(SchemaModel):
    card: ReproductionCard
    plan: ExperimentPlan


class CodegenResponse(SchemaModel):
    bundle: CodeBundle


class ValidationIssue(SchemaModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None


class ValidationReport(SchemaModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class StaticValidateRequest(SchemaModel):
    bundle: CodeBundle


class ErrorClassification(SchemaModel):
    category: ErrorCategory
    reason: str = Field(min_length=1)
    repairable: bool = True


class ClassifyRequest(SchemaModel):
    stderr: str = ""
    stdout: str = ""
    exit_code: int | None = None
    status: RunStatus | None = None


class RepairRequest(SchemaModel):
    bundle: CodeBundle
    classification: ErrorClassification
    plan: ExperimentPlan


class RepairResponse(SchemaModel):
    bundle: CodeBundle
    change_reason: str = Field(min_length=1)
    replan_required: bool = False
