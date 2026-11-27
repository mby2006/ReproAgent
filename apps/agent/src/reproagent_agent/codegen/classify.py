"""Deterministic error classification from execution output.

Maps stderr/stdout/exit status onto the eight ErrorCategory values so the repair
loop can branch without an LLM. Order matters: terminal status and OOM are
checked before the more generic shape/metric patterns.
"""

import re

from reproagent_schemas import ClassifyRequest, ErrorCategory, ErrorClassification, RunStatus

_PATTERNS: list[tuple[ErrorCategory, re.Pattern[str]]] = [
    (ErrorCategory.SYNTAX_ERROR, re.compile(r"SyntaxError|IndentationError|TabError", re.I)),
    (
        ErrorCategory.IMPORT_ERROR,
        re.compile(r"ModuleNotFoundError|ImportError|No module named|cannot import name", re.I),
    ),
    (
        ErrorCategory.OUT_OF_MEMORY,
        re.compile(
            r"out of memory|OOM|CUDA error|DefaultCPUAllocator|can't allocate|"
            r"cannot allocate|ENOMEM|Killed|std::bad_alloc",
            re.I,
        ),
    ),
    (
        ErrorCategory.SHAPE_MISMATCH,
        re.compile(
            r"size mismatch|shape|mat1 and mat2|expected input|dimension|"
            r"invalid shape|sizes of tensors|must match the size",
            re.I,
        ),
    ),
    (
        ErrorCategory.DATA_DOWNLOAD_ERROR,
        re.compile(
            r"download|ConnectionError|URLError|HTTPError|Connection refused|"
            r"Failed to establish|Network is unreachable|getaddrinfo|timed out",
            re.I,
        ),
    ),
    (
        ErrorCategory.METRIC_ERROR,
        re.compile(
            r"metrics\.json|metric_value|target_metric|MetricsReport|"
            r"KeyError: 'metric|validation error|schema_version",
            re.I,
        ),
    ),
]

_REPAIRABLE = {
    ErrorCategory.SYNTAX_ERROR,
    ErrorCategory.IMPORT_ERROR,
    ErrorCategory.SHAPE_MISMATCH,
    ErrorCategory.OUT_OF_MEMORY,
    ErrorCategory.DATA_DOWNLOAD_ERROR,
    ErrorCategory.METRIC_ERROR,
}


def classify_error(request: ClassifyRequest) -> ErrorClassification:
    if request.status is RunStatus.TIMEOUT:
        return ErrorClassification(
            category=ErrorCategory.TIMEOUT,
            reason="run exceeded its hard time budget and was killed",
            # A hard timeout is a terminal resource outcome, not a code defect.
            # Retrying it would replace TIMEOUT with a later repair's result.
            repairable=False,
        )
    if request.exit_code in (137, -9):
        return ErrorClassification(
            category=ErrorCategory.OUT_OF_MEMORY,
            reason=f"process killed by signal (exit_code={request.exit_code}), likely OOM",
            repairable=True,
        )
    haystack = f"{request.stderr}\n{request.stdout}"
    for category, pattern in _PATTERNS:
        match = pattern.search(haystack)
        if match:
            return ErrorClassification(
                category=category,
                reason=f"matched error signature: {match.group(0)!r}",
                repairable=category in _REPAIRABLE,
            )
    return ErrorClassification(
        category=ErrorCategory.UNKNOWN_ERROR,
        reason="no known error signature matched",
        repairable=False,
    )