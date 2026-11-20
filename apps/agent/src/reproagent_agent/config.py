from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent holds the LLM key only. No Supabase keys may appear here."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    runner_base_url: str = "http://127.0.0.1:8002"
    review_cache_max_entries: int = Field(default=1024, ge=1, le=10_000)
    review_cache_ttl_seconds: float = Field(default=300, gt=0, le=86_400, allow_inf_nan=False)
    review_selection_threshold: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
