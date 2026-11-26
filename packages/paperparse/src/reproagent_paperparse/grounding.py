"""Evidence grounding: verify LLM quotes against the parsed document.

The LLM proposes values and quotes; this module decides provenance.
source_page / source_section are ALWAYS taken from the parser's match location,
never from the LLM output. Grounding has two orthogonal layers:

1. Location (QuoteLocator): does the quote text exist in the document?
2. Support (semantic): does the located quote actually support the claimed
   field value?  A located-but-unsupported quote is rejected: the accepted
   citation is cleared and the real location is stored in candidate_* fields
   for human review only -- never as paper evidence.

Outcomes per field: a quote that cannot be located is ``citation_not_found``;
a located quote that fails its field-type semantic conditions is
``located_unsupported``; only a located *and* supporting quote is ``verified``.
Rejections keep status inferred (value preserved) or demote to missing when no
value exists, never paper_explicit/paper_described.
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from reproagent_schemas import OPTIMIZER_NAMES, ParsedDocument

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
        "grounding_status": "not_checked",
        "candidate_quote": None,
        "candidate_page": None,
        "candidate_section": None,
        "grounding_reason": None,
    }


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(m.group()) for m in _NUMBER_RE.finditer(text)]


def _value_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _mentions_value(v_norm: str, q_norm: str) -> bool:
    """Loose entity test: does the quote mention the value's distinctive word?

    The value phrase itself, or at least one substantive value token (>= 4
    letters, or a digit such as "5" in "LeNet-5"), must occur in the quote.
    Morphological variants still count via substring matching ("layers" finds
    "layer"). Stopword-only values never pass, so a purely structural quote
    ("Layer C3 has 16 feature maps.") cannot ground an unrelated entity.
    """
    if not v_norm:
        return False
    if v_norm in q_norm:
        return True
    return any(
        len(token) >= 4 or any(char.isdigit() for char in token)
        for token in v_norm.split()
        if token in q_norm
    )


def _support_reason(field_type: str, value: object, quote: str) -> str | None:
    """Conservative semantic check: why the located quote may not support the value.

    Returns None when the quote carries the limited, explicit conditions the
    field type requires; a reason string otherwise. Unknown field types are not
    semantically classified (location alone decides), matching prior behaviour.
    """
    q_norm = normalize(quote)
    q_tokens = set(q_norm.split())
    v_norm = normalize(str(value)) if value is not None else ""
    v_number = _value_number(value)

    def value_missing() -> str:
        return f"引用未提及字段值 {value!r} 的实体词"

    match field_type:
        case "dataset" | "method" | "model_architecture":
            if _mentions_value(v_norm, q_norm):
                return None
            return value_missing()
        case "optimizer":
            named = "optimizer" in q_tokens or any(name in q_norm for name in OPTIMIZER_NAMES)
            if named and (not v_norm or _mentions_value(v_norm, q_norm)):
                return None
            return "引用缺少优化器名称或 optimizer 语境"
        case "learning_rate":
            lr_ctx = "learning rate" in q_norm or "lr" in q_tokens
            if not lr_ctx:
                return "引用缺少 learning rate/LR 语境"
            if v_number is None:
                return None
            q_numbers = _numbers(quote)
            tol = abs(v_number) * 1e-6
            if any(abs(n - v_number) <= tol for n in q_numbers):
                return None
            return f"引用未出现与字段值相同数量级的学习率数值 ({v_number:g})"
        case "loss_function":
            if "loss" in q_tokens or "objective" in q_tokens or "cross entropy" in q_norm:
                return None
            return "引用缺少 loss/objective/cross-entropy 函数语境"
        case "batch_size":
            if "batch" not in q_tokens:
                return "引用缺少 batch 语境"
            if v_number is None:
                return None
            if any(abs(n - v_number) <= abs(v_number) * 1e-6 for n in _numbers(quote)):
                return None
            return f"引用 batch 语境中未出现数值 {v_number:g}"
        case "epochs":
            if not ({"epoch", "epochs"} & q_tokens):
                return "引用缺少 epoch 语境"
            if v_number is None:
                return None
            if any(abs(n - v_number) <= abs(v_number) * 1e-6 for n in _numbers(quote)):
                return None
            return f"引用 epoch 语境中未出现数值 {v_number:g}"
        case "metric":
            if not q_tokens & {
                "metric",
                "metrics",
                "accuracy",
                "error",
                "rate",
                "f1",
                "auc",
                "loss",
                "recall",
                "precision",
            }:
                return "引用缺少 metric 名称"
            if v_number is None:
                return None
            if _numbers(quote):
                return None
            return "引用未出现任何数值，无法构成 metric/数值支持关系"
        case _:
            return None


def _semantic_type(name: str, field: dict) -> str:
    if field.get("metric"):
        return "metric"
    if name == "evaluation_metrics":
        return "metric"
    return name


def _rejected(
    field: dict,
    *,
    grounding_status: str,
    candidate_quote: str | None,
    candidate_page: int | None,
    candidate_section: str | None,
    grounding_reason: str,
    confidence: float,
) -> dict:
    """Clear the accepted citation; keep value and store the candidate for review."""
    return {
        **field,
        "status": "inferred",
        "source_page": None,
        "source_section": None,
        "evidence_quote": None,
        "grounding_status": grounding_status,
        "candidate_quote": candidate_quote,
        "candidate_page": candidate_page,
        "candidate_section": candidate_section,
        "grounding_reason": grounding_reason,
        "confidence": min(confidence, 0.4),
        "human_confirmed": False,
    }


def _ground_field(field: dict, locator: QuoteLocator, name: str) -> tuple[dict, bool]:
    """Return (grounded field, demoted_to_missing)."""
    confidence = min(max(float(field.get("confidence") or 0.0), 0.0), 1.0)
    status = field.get("status")
    value = field.get("value")
    quote = field.get("evidence_quote")
    has_quote = isinstance(quote, str) and quote.strip()

    if status == "missing" or (value is None and not has_quote):
        return _missing_field(confidence), status != "missing"

    if has_quote:
        match = locator.locate(quote)
        if match is not None:
            reason = _support_reason(_semantic_type(name, field), value, quote)
            if reason is None:
                grounded = {
                    **field,
                    "status": status
                    if status in ("paper_explicit", "paper_described", "inferred")
                    else "inferred",
                    "source_page": match.page,
                    "source_section": match.section,
                    "evidence_quote": quote.strip(),
                    "confidence": confidence,
                    "human_confirmed": False,
                    "grounding_status": "verified",
                    "candidate_quote": None,
                    "candidate_page": None,
                    "candidate_section": None,
                    "grounding_reason": None,
                }
                return grounded, False
            return (
                _rejected(
                    field,
                    grounding_status="located_unsupported",
                    candidate_quote=quote.strip(),
                    candidate_page=match.page,
                    candidate_section=match.section,
                    grounding_reason=f"引用已定位但不支持字段值：{reason}",
                    confidence=confidence,
                ),
                False,
            )
        if value is None:
            return _missing_field(confidence), True
        claimed = field.get("source_page")
        claimed_page = (
            claimed
            if isinstance(claimed, int) and not isinstance(claimed, bool) and claimed >= 1
            else None
        )
        return (
            _rejected(
                field,
                grounding_status="citation_not_found",
                candidate_quote=quote.strip(),
                candidate_page=claimed_page,
                candidate_section=None,
                grounding_reason="引用文本无法在论文中定位",
                confidence=confidence,
            ),
            False,
        )

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
            "grounding_status": "not_checked",
            "candidate_quote": None,
            "candidate_page": None,
            "candidate_section": None,
            "grounding_reason": None,
        },
        False,
    )


def ground_card(card: dict, document: ParsedDocument) -> dict:
    """Ground every evidence field of a raw LLM card dict; safe to model_validate afterwards."""
    locator = QuoteLocator(document)
    grounded = dict(card)
    demoted: list[str] = []
    rejected: list[tuple[str, dict]] = []

    def handle(field: dict, path: str, name: str) -> dict:
        result, to_missing = _ground_field(field, locator, name)
        if to_missing:
            demoted.append(path)
        if result.get("grounding_status") in ("citation_not_found", "located_unsupported"):
            rejected.append((path, result))
        return result

    for name in _SCALAR_FIELDS:
        if isinstance(grounded.get(name), dict):
            grounded[name] = handle(grounded[name], name, name)
    for name in _DICT_FIELDS:
        entries = grounded.get(name)
        if isinstance(entries, dict):
            grounded[name] = {
                key: handle(field, f"{name}.{key}", key)
                for key, field in entries.items()
                if isinstance(field, dict)
            }
    for name in _LIST_FIELDS:
        items = grounded.get(name)
        if isinstance(items, list):
            grounded[name] = [
                handle(field, f"{name}[{i}]", name)
                for i, field in enumerate(items)
                if isinstance(field, dict)
            ]

    missing_info = [str(item) for item in grounded.get("missing_information") or []]
    missing_info.extend(f"{path}: no verifiable evidence found in the paper" for path in demoted)
    missing_info.extend(
        f"{path}: candidate citation is reviewable but was not accepted as paper "
        f"evidence ({result.get('grounding_reason') or 'unverifiable'})"
        for path, result in rejected
    )
    grounded["missing_information"] = missing_info
    return grounded
