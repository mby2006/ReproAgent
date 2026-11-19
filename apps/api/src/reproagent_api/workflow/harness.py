"""Harness primitives: credential redaction, budget enforcement, trace keys.

Traces must never contain credentials. ``redact`` drops sensitive keys outright
and masks secret-shaped string values, then truncates long strings so a trace
row stays bounded. Budget helpers raise ``BudgetExceeded`` when a ceiling is hit;
the workflow driver turns that into a terminal ``failed`` status with state saved.
"""

import re
from uuid import UUID

from reproagent_schemas import BudgetConfig, BudgetState, LLMUsage, SchemaModel

MAX_TRACE_STRING = 2000

_SENSITIVE_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "service_role",
    "anon_key",
)
_SENSITIVE_KEY_EXACT = {
    "token",
    "access_token",
    "refresh_token",
    "pdf_b64",
    "supabase_service_role_key",
    "supabase_anon_key",
}
_SECRET_VALUE_RE = re.compile(
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|sb_secret_[A-Za-z0-9_\-]+"
    r"|sb_publishable_[A-Za-z0-9_\-]+"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
)

# Compact, credential-free view of state recorded as a node's trace input.
TRACE_INPUT_KEYS = (
    "workflow_id",
    "paper_id",
    "experiment_id",
    "run_id",
    "status",
    "current_node",
    "repair_attempt",
    "replan_count",
    "code_version",
    "plan_version",
    "spec_version",
)
# Large or sensitive payloads excluded from trace output (still truncated below).
TRACE_OUTPUT_DROP = {"document", "pdf_b64", "card_raw", "usages"}
# State updates mirrored onto the workflows row after each node completes.
WORKFLOW_PERSIST_KEYS = (
    "status",
    "experiment_id",
    "run_id",
    "spec_version_id",
    "plan_id",
    "error",
)


class BudgetExceeded(Exception):
    """A configured budget ceiling (LLM calls / tokens / experiment time) was hit."""


def redact(obj):
    if isinstance(obj, SchemaModel):
        obj = obj.model_dump(mode="json")
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_KEY_EXACT or any(
                fragment in lowered for fragment in _SENSITIVE_KEY_SUBSTRINGS
            ):
                continue
            out[key] = redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        masked = _SECRET_VALUE_RE.sub("[REDACTED]", obj)
        if len(masked) > MAX_TRACE_STRING:
            masked = masked[:MAX_TRACE_STRING] + "…[truncated]"
        return masked
    if isinstance(obj, UUID):
        return str(obj)
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    return str(obj)


def trace_input(state: dict) -> dict:
    return redact({key: state.get(key) for key in TRACE_INPUT_KEYS})


def trace_output(updates: dict) -> dict:
    return redact({k: v for k, v in updates.items() if k not in TRACE_OUTPUT_DROP})


def ensure_llm_budget(state: dict, config: BudgetConfig) -> None:
    budget: BudgetState = state["budget"]
    if budget.llm_calls >= config.max_llm_calls:
        raise BudgetExceeded(
            f"LLM call budget exhausted ({budget.llm_calls}/{config.max_llm_calls})"
        )


def record_usages(state: dict, config: BudgetConfig, usages: list[LLMUsage]) -> BudgetState:
    budget = state["budget"].model_copy(deep=True)
    for usage in usages:
        budget.record_llm(usage)
    if budget.total_tokens > config.max_total_tokens:
        raise BudgetExceeded(
            f"token budget exhausted ({budget.total_tokens}/{config.max_total_tokens})"
        )
    return budget


def time_budget_exhausted(state: dict, config: BudgetConfig) -> bool:
    budget: BudgetState = state["budget"]
    return budget.experiment_seconds >= config.max_experiment_seconds
