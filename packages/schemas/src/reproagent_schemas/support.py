"""Shared contract for the deterministic paper support gate.

Used by the Agent planner (``check_supported``) and the API agent client
(``check_support``) so the two sides of the HTTP boundary exchange a single,
identical model instead of each defining its own. A plain frozen dataclass is
used (not a pydantic model) so it stays immutable and the wire DTO in the Agent
router can remain the source of truth for JSON validation.
"""

from dataclasses import dataclass
from typing import Literal

SupportCode = Literal["SUPPORTED", "SCANNED_PDF", "UNSUPPORTED_DATASET", "UNSUPPORTED_MODEL"]


@dataclass(frozen=True)
class PaperSupportCheck:
    """Outcome of the deterministic paper support gate.

    ``code`` is one of :data:`SupportCode`. ``message`` is a stable,
    human-readable reason safe to persist on a failed workflow row.
    """

    supported: bool
    code: str
    message: str


__all__ = ["PaperSupportCheck", "SupportCode"]
