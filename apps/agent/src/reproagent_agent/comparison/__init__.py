"""Deterministic comparison support: candidates, planning, verification, report.

No LLM is involved. Every value either comes from grounded Card evidence, from
an explicitly labelled user decision, or from the tagged MVP scaling profile.
"""

from .candidates import find_candidates
from .planner import UnsupportedComparisonError, build_comparison_plan
from .report import build_comparison_report
from .support import SUPPORTED_METRICS, support_check
from .verify import verify_pairs

__all__ = [
    "SUPPORTED_METRICS",
    "UnsupportedComparisonError",
    "build_comparison_plan",
    "build_comparison_report",
    "find_candidates",
    "support_check",
    "verify_pairs",
]
