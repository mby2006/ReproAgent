from openai import AsyncOpenAI
from reproagent_schemas import LLMUsage

from .base import ChatMessage, LLMAdapter, LLMResponse


class QwenAdapter(LLMAdapter):
    """Qwen via the DashScope OpenAI-compatible endpoint."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.model = model
        # Construction must succeed without a key so the service can boot and
        # report health; actual calls fail with an auth error until configured.
        self._client = AsyncOpenAI(api_key=api_key or "not-configured", base_url=base_url)

    async def complete_with_usage(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("LLM returned empty content")
        usage = LLMUsage(model_name=self.model)
        if response.usage is not None:
            usage.prompt_tokens = response.usage.prompt_tokens or 0
            usage.completion_tokens = response.usage.completion_tokens or 0
            usage.total_tokens = response.usage.total_tokens or 0
        return LLMResponse(content=content, usage=usage)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        result = await self.complete_with_usage(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        return result.content
