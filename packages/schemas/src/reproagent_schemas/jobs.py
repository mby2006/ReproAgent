from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .common import IdentifiedModel, SchemaModel, utcnow
from .plan import ExperimentPlan
from .spec import ReproductionSpec


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobAccepted(SchemaModel):
    """Returned with HTTP 202 for every long-running task."""

    job_id: UUID
    status: JobStatus = JobStatus.QUEUED
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ReproductionJob(IdentifiedModel):
    paper_id: UUID
    status: JobStatus = JobStatus.QUEUED
    spec: ReproductionSpec | None = None
    plan: ExperimentPlan | None = None
    error: str | None = None
