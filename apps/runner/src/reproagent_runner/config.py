from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunnerSettings(BaseSettings):
    """Deliberately contains no API keys or tokens of any kind."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="RUNNER_", extra="ignore"
    )

    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    work_dir: Path = Path("./.runner-work")
    timeout_seconds: int = 300
    max_output_chars: int = 20000

    exec_image: str = "reproagent-exec:latest"
    docker_host: str | None = None
    sandbox_user: str = "1000:1000"
    enforce_disk_quota: bool = True
    tmpfs_size_mb: int = 64
    max_concurrent_runs: int = Field(default=4, gt=0, le=64)
    run_ttl_seconds: int = Field(default=3600, gt=0)
    reap_interval_seconds: int = Field(default=60, gt=0)


@lru_cache
def get_settings() -> RunnerSettings:
    return RunnerSettings()