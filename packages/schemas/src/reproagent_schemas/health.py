from datetime import datetime
from enum import StrEnum

from pydantic import Field

from .common import SchemaModel, utcnow


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HealthResponse(SchemaModel):
    service: str
    status: HealthStatus = HealthStatus.OK
    version: str = "0.1.0"
    timestamp: datetime = Field(default_factory=utcnow)
    details: dict[str, str | bool | int | float | None] = Field(default_factory=dict)


class DependencyHealth(SchemaModel):
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


class DependenciesHealthResponse(SchemaModel):
    service: str
    status: HealthStatus
    dependencies: list[DependencyHealth]
    timestamp: datetime = Field(default_factory=utcnow)
