from ..config import AgentSettings
from .base import LLMAdapter
from .qwen import QwenAdapter


def get_llm_adapter(settings: AgentSettings) -> LLMAdapter:
    return QwenAdapter(
        api_key=settings.qwen_api_key,
        base_url=settings.qwen_base_url,
        model=settings.qwen_model,
    )
