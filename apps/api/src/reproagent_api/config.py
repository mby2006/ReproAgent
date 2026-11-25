from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    supabase_url: str = "http://127.0.0.1:54321"
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_db_url: str = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
    # PDF 的 capability 链接用它的 HMAC 派生签名。生产上就是自托管 Supabase 的
    # JWT_SECRET（compose 里 PDF_LINK_SECRET: ${JWT_SECRET}）—— 之所以复用而不是
    # 再起一个机密：这个签名只需要「除本机以外无人知道」，不需要独立轮换域。
    pdf_link_secret: str = ""
    agent_base_url: str = "http://127.0.0.1:8001"
    runner_base_url: str = "http://127.0.0.1:8002"
    redis_url: str = "redis://127.0.0.1:6379"
    ops_token: str = ""
    cors_origins: str = "http://localhost:3000"

    @field_validator("cors_origins")
    @classmethod
    def strip_origins(cls, value: str) -> str:
        return ",".join(origin.strip() for origin in value.split(",") if origin.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return self.cors_origins.split(",") if self.cors_origins else []


@lru_cache
def get_settings() -> ApiSettings:
    return ApiSettings()
