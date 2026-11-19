from abc import ABC, abstractmethod
from typing import Literal

from pydantic import Field
from reproagent_schemas import LLMUsage, SchemaModel


class ChatMessage(SchemaModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class LLMResponse(SchemaModel):
    content: str
    usage: LLMUsage = Field(default_factory=LLMUsage)


class LLMAdapter(ABC):
    """Unified LLM interface; providers are swappable without touching graph code."""

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str: ...

    async def complete_with_usage(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Default: delegate to complete() with zeroed usage (stubs/tests)."""
        content = await self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        return LLMResponse(content=content, usage=LLMUsage())
