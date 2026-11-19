"""Evidence grounding: verify LLM quotes against the parsed document.

The LLM proposes values and quotes; this module decides provenance.
source_page / source_section are ALWAYS taken from the parser's match location,
never from the LLM output. A paper_explicit claim whose quote cannot be located
is demoted to inferred (value kept, evidence stripped) or missing (no value).
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from reproagent_schemas import ParsedDocument

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"}
_MIN_QUOTE_CHARS = 4
_FUZZY_COVERAGE = 0.85
_FUZZY_TOKEN_RECALL = 0.8

_SCALAR_FIELDS = ("method", "model_architecture", "dataset", "optimizer", "loss_function")
_DICT_FIELDS = ("data_preprocessing", "hyperparameters")
_LIST_FIELDS = ("evaluation_metrics", "reported_results", "baselines")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for lig, repl in _LIGATURES.items():
        text = text.replace(lig, repl)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass(frozen=True)
class QuoteMatch:
    page: int
    section: str | None
    block_id: str
    score: float


@dataclass(frozen=True)
class _Entry:
    block_id: str
    page: int
    section: str | None
    norm: str
    tokens: frozenset[str]


class QuoteLocator:
    def __init__(self, document: ParsedDocument):
        self._entries: list[_Entry] = []
        for block in document.blocks:
            norm = normalize(block.text)
            if norm:
                self._entries.append(
                    _Entry(block.id, block.page, block.section, norm, frozenset(norm.split()))
                )
        for table in document.tables:
            norm = normalize(" ".join(" ".join(row) for row in table.rows))
            if norm:
                self._entries.append(
                    _Entry(table.id, table.page, table.section, norm, frozenset(norm.split()))
                )
        self._pages: dict[int, list[_Entry]] = {}
        for entry in self._entries:
            self._pages.setdefault(entry.page, []).append(entry)

    def locate(self, quote: str) -> QuoteMatch | None:
        q = normalize(quote)
        if len(q) < _MIN_QUOTE_CHARS:
            return None
        for entry in self._entries:
            if q in entry.norm:
                return QuoteMatch(entry.page, entry.section, entry.block_id, 1.0)
        for page, entries in self._pages.items():
            joined = " ".join(e.norm for e in entries)
            pos = joined.find(q)
            if pos >= 0:
                offset = 0
                for entry in entries:
                    if offset + len(entry.norm) > pos:
                        return QuoteMatch(page, entry.section, entry.block_id, 1.0)
                    offset += len(entry.norm) + 1
        return self._fuzzy(q)

    def _fuzzy(self, q: str) -> QuoteMatch | None:
        q_tokens = set(q.split())
        best: QuoteMatch | None = None
        for entry in self._entries:
            if not q_tokens:
                break
            recall = len(q_tokens & entry.tokens) / len(q_tokens)
            if recall < _FUZZY_TOKEN_RECALL:
                continue
            matcher = SequenceMatcher(None, q, entry.norm, autojunk=False)
            matched = sum(m.size for m in matcher.get_matching_blocks())
            coverage = matched / len(q)
            if coverage >= _FUZZY_COVERAGE and (best is None or coverage > best.score):
                best = QuoteMatch(entry.page, entry.section, entry.block_id, coverage)
        return best


def _missing_field(confidence: float = 0.0) -> dict:
    return {
        "value": None,
        "status": "missing",
        "source_page": None,
        "source_section": None,
        "evidence_quote": None,
        "confidence": confidence,
        "human_confirmed": False,
    }


def _ground_field(field: dict, locator: QuoteLocator) -> tuple[dict, bool]:
    """Return (grounded field, demoted_to_missing)."""
    confidence = min(max(float(field.get("confidence") or 0.0), 0.0), 1.0)
    status = field.get("status")
    value = field.get("value")
    quote = field.get("evidence_quote")

    if status == "missing" or (value is None and not quote):
        return _missing_field(confidence), status != "missing"

    if isinstance(quote, str) and quote.strip():
        match = locator.locate(quote)
        if match is not None:
            grounded = {
                **field,
                "status": status if status in ("paper_explicit", "inferred") else "inferred",
                "source_page": match.page,
                "source_section": match.section,
                "evidence_quote": quote.strip(),
                "confidence": confidence,
                "human_confirmed": False,
            }
            return grounded, False

    if value is None:
        return _missing_field(confidence), True
    return (
        {
            **field,
            "value": value,
            "status": "inferred",
            "source_page": None,
            "source_section": None,
            "evidence_quote": None,
            "confidence": min(confidence, 0.4),
            "human_confirmed": False,
        },
        False,
    )


def ground_card(card: dict, document: ParsedDocument) -> dict:
    """Ground every evidence field of a raw LLM card dict; safe to model_validate afterwards."""
    locator = QuoteLocator(document)
    grounded = dict(card)
    demoted: list[str] = []

    def handle(field: dict, path: str) -> dict:
        result, to_missing = _ground_field(field, locator)
        if to_missing:
            demoted.append(path)
        return result

    for name in _SCALAR_FIELDS:
        if isinstance(grounded.get(name), dict):
            grounded[name] = handle(grounded[name], name)
    for name in _DICT_FIELDS:
        entries = grounded.get(name)
        if isinstance(entries, dict):
            grounded[name] = {
                key: handle(field, f"{name}.{key}")
                for key, field in entries.items()
                if isinstance(field, dict)
            }
    for name in _LIST_FIELDS:
        items = grounded.get(name)
        if isinstance(items, list):
            grounded[name] = [
                handle(field, f"{name}[{i}]")
                for i, field in enumerate(items)
                if isinstance(field, dict)
            ]

    missing_info = [str(item) for item in grounded.get("missing_information") or []]
    missing_info.extend(f"{path}: no verifiable evidence found in the paper" for path in demoted)
    grounded["missing_information"] = missing_info
    return grounded
