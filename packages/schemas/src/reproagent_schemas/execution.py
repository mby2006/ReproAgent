"""Contracts for sandboxed execution (Agent/API -> Runner) and code generation.

The Runner never receives host credentials and never executes code in its own
process: it spawns a locked-down sibling container per run. Dependencies are a
fixed, pinned whitelist -- arbitrary `pip install` is forbidden. Every run emits
metrics against a deterministic JSON schema (MetricsReport) so the Evaluator can
read them without guessing.
"""

import hashlib
import re
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from .common import SchemaModel

# Pinned dependency whitelist. The exec image installs exactly these versions;
# generated requirements.txt and RunRequest.dependencies must match verbatim.
DEPENDENCY_WHITELIST: dict[str, str] = {
    "torch": "2.4.1",
    "torchvision": "0.19.1",
    "numpy": "1.26.4",
    "pyyaml": "6.0.2",
}

_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ErrorCategory(StrEnum):
    SYNTAX_ERROR = "SYNTAX_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    SHAPE_MISMATCH = "SHAPE_MISMATCH"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    TIMEOUT = "TIMEOUT"
    DATA_DOWNLOAD_ERROR = "DATA_DOWNLOAD_ERROR"
    METRIC_ERROR = "METRIC_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


def is_whitelisted(requirement: str) -> bool:
    name, sep, version = requirement.partition("==")
    return bool(sep) and DEPENDENCY_WHITELIST.get(name.strip()) == version.strip()


class ResourceBudget(SchemaModel):
    """Hard ceilings enforced by the sandbox. max_seconds caps at 10 minutes."""

    max_seconds: int = Field(default=600, gt=0, le=600)
    memory_mb: int = Field(default=512, gt=0, le=4096)
    cpu_count: float = Field(default=1.0, gt=0, le=4.0)
    pids_limit: int = Field(default=256, gt=0, le=1024)
    disk_mb: int = Field(default=256, gt=0, le=2048)
    device: str = "cpu"


class CodeFile(SchemaModel):
    path: str = Field(min_length=1, max_length=200)
    content: str
    sha256: str = ""

    @model_validator(mode="after")
    def check_path_and_hash(self) -> "CodeFile":
        if not _SAFE_PATH.match(self.path) or ".." in self.path or self.path.startswith("/"):
            raise ValueError(f"unsafe code file path: {self.path}")
        if not self.sha256:
            self.sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        return self


class RunRequest(SchemaModel):
    experiment_id: UUID
    experiment_plan_id: UUID
    code_files: list[CodeFile] = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    resource_budget: ResourceBudget = Field(default_factory=ResourceBudget)
    entrypoint: str = Field(default="train.py", min_length=1)
    seed: int = 42
    code_version: int = Field(default=1, ge=1)
    plan_version: int = Field(default=1, ge=1)
    metrics_filename: str = "metrics.json"

    @model_validator(mode="after")
    def check_dependencies_and_entry(self) -> "RunRequest":
        for requirement in self.dependencies:
            if not is_whitelisted(requirement):
                raise ValueError(f"dependency not in pinned whitelist: {requirement}")
        paths = {f.path for f in self.code_files}
        if self.entrypoint not in paths:
            raise ValueError(f"entrypoint '{self.entrypoint}' not present in code_files")
        if (
            "/" in self.metrics_filename
            or "\\" in self.metrics_filename
            or self.metrics_filename in ("", ".", "..")
        ):
            raise ValueError(f"unsafe metrics_filename: {self.metrics_filename}")
        return self


class RunAccepted(SchemaModel):
    run_id: UUID
    status: RunStatus = RunStatus.QUEUED


class RunAudit(SchemaModel):
    """Reproducibility fingerprint persisted with every run."""

    image_digest: str | None = None
    python_version: str | None = None
    dependency_lock_hash: str | None = None
    dataset_fingerprint: str | None = None
    seed: int | None = None
    code_hash: str | None = None
    plan_version: int | None = None
    code_version: int | None = None


class RunLogs(SchemaModel):
    # raw log text: leading/trailing whitespace is content, not noise
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    stdout: str = ""
    stderr: str = ""


class RunResult(SchemaModel):
    run_id: UUID
    status: RunStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    execution_time: float | None = Field(default=None, ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error_category: ErrorCategory | None = None
    error: str | None = None
    audit: RunAudit | None = None


class MetricsReport(SchemaModel):
    """Deterministic metrics.json schema written by generated training code."""

    schema_version: str = "1.0"
    target_metric: str = Field(min_length=1)
    metric_value: float
    accuracy: float | None = None
    train_loss: float | None = None
    test_loss: float | None = None
    epochs_completed: int = Field(ge=0)
    train_samples: int = Field(ge=0)
    test_samples: int = Field(ge=0)
    seed: int
    runtime_seconds: float = Field(ge=0)
