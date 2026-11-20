"""LLM extraction with grounding and bounded retries.

Split into two stages so the workflow can trace them separately:
- extract_raw: LLM proposes a card (bounded corrective retries for invalid JSON
  or schema failures), returning the raw dict plus per-call token usage.
- ground_and_validate: deterministic parser-assigned provenance + strict Pydantic
  validation. No LLM here -- evidence grounding can never be fabricated.

extract_card remains the composite used by the legacy /v1/understand endpoint.
"""

import json
from copy import deepcopy

from pydantic import ValidationError
from reproagent_paperparse import ground_card
from reproagent_schemas import LLMUsage, ParsedDocument, ReproductionCard

from ..llm.base import ChatMessage, LLMAdapter
from .prompts import SYSTEM_PROMPT, build_user_prompt

MAX_ATTEMPTS = 2


class UnderstandingError(Exception):
    pass


def _parse_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in LLM output")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object")
    return parsed


def _without_review_metadata(raw: dict) -> dict:
    """Untrusted extraction cannot attest to an API review."""
    data = deepcopy(raw)
    metadata = (
        "review_mode", "review_selection_threshold", "delegated_at", "delegated_by",
        "review_decisions", "human_confirmed", "confirmed_by",
    )
    fields = [data]
    fields.extend(data.get(key) for key in (
        "method", "model_architecture", "dataset", "optimizer", "loss_function",
    ))
    for key in ("data_preprocessing", "hyperparameters"):
        if isinstance(data.get(key), dict):
            fields.extend(data[key].values())
    for key in ("evaluation_metrics", "reported_results", "baselines"):
        if isinstance(data.get(key), list):
            fields.extend(data[key])
    for field in fields:
        if isinstance(field, dict):
            for key in metadata:
                field.pop(key, None)
    return data


def ground_and_validate(raw: dict, document: ParsedDocument) -> ReproductionCard:
    """Deterministic evidence grounding + strict validation (no LLM)."""
    grounded = ground_card(_without_review_metadata(raw), document)
    try:
        return ReproductionCard.model_validate(grounded)
    except ValidationError as exc:
        raise UnderstandingError(f"schema validation failed: {exc}") from exc


async def extract_raw(
    llm: LLMAdapter,
    document: ParsedDocument,
    title_hint: str | None = None,
) -> tuple[dict, list[LLMUsage]]:
    """LLM proposes a raw card; returns (raw_dict, per-call usages).

    Bounded corrective retries cover invalid JSON and grounding/schema failures.
    The returned raw dict is guaranteed to ground+validate successfully.
    """
    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=build_user_prompt(document, title_hint)),
    ]
    usages: list[LLMUsage] = []
    last_error = "unknown"
    for _ in range(MAX_ATTEMPTS):
        response = await llm.complete_with_usage(messages, temperature=0.1, max_tokens=8192)
        usages.append(response.usage)
        reply = response.content
        try:
            raw = _without_review_metadata(_parse_json(reply))
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"invalid JSON: {exc}"
            messages.append(ChatMessage(role="assistant", content=reply))
            messages.append(
                ChatMessage(
                    role="user",
                    content=f"Your output was not valid JSON ({exc}). "
                    "Return ONLY the JSON object described in the system prompt.",
                )
            )
            continue
        try:
            ground_and_validate(raw, document)
        except UnderstandingError as exc:
            last_error = str(exc)
            messages.append(ChatMessage(role="assistant", content=reply))
            messages.append(
                ChatMessage(
                    role="user",
                    content="The card failed schema validation:\n"
                    f"{str(exc)[:2000]}\nFix the JSON and return it again. "
                    "Remember: missing fields need value null and status 'missing'.",
                )
            )
            continue
        return raw, usages
    raise UnderstandingError(f"card extraction failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def extract_card(
    llm: LLMAdapter,
    document: ParsedDocument,
    title_hint: str | None = None,
) -> ReproductionCard:
    raw, _ = await extract_raw(llm, document, title_hint)
    return ground_and_validate(raw, document)
