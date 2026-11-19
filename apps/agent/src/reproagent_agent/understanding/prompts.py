"""Prompt construction for Reproduction Card extraction.

The LLM sees page/section markers but is told NOT to emit page numbers:
provenance is assigned by deterministic grounding, never by the model.
"""

from reproagent_schemas import ParsedDocument

SYSTEM_PROMPT = """\
You are a scientific-paper reproduction analyst. You extract a structured
"Reproduction Card" from the provided paper text.

Output ONLY a valid JSON object, no markdown fences, no commentary, with exactly
these top-level keys:
  method, model_architecture, dataset, data_preprocessing, hyperparameters,
  optimizer, loss_function, evaluation_metrics, reported_results, baselines,
  missing_information

Field shapes:
- method / model_architecture / dataset / optimizer / loss_function:
  an "evidence field" object (see below), value is a string.
- data_preprocessing: object mapping snake_case keys to evidence fields (string values).
- hyperparameters: object mapping snake_case keys (e.g. learning_rate, batch_size,
  epochs, weight_decay) to evidence fields whose value is a number, string or boolean.
- evaluation_metrics: array of evidence fields with string values (e.g. "error_rate").
- reported_results: array of evidence fields with numeric values, plus extra keys
  "metric" (string, required) and "condition" (string or null, e.g. "MNIST test set").
- baselines: array of evidence fields with string values describing the comparison
  method/result, plus extra key "name" (string, required).
- missing_information: array of strings naming important reproduction details that
  the paper does not state.

Every evidence field object has exactly these keys:
  value, status, evidence_quote, confidence, human_confirmed
Rules for evidence fields:
- status is one of "paper_explicit", "paper_described", "inferred", "missing".
- paper_explicit: the paper states it directly. value is set and evidence_quote MUST
  be a VERBATIM contiguous excerpt copied from the paper text below (identical wording;
  keep it under 60 words). Never paraphrase a quote.
- paper_described: the paper describes this practice in prose (typically a preprocessing
  step such as centering, size normalization or background extension) rather than as a
  verbatim numeric parameter. value is set; evidence_quote MUST be a VERBATIM excerpt of
  the describing sentence. Use this instead of "inferred" whenever the paper actually
  describes the practice, so it is not flagged as an unsupported guess.
- inferred: you deduce it from context but the paper does not state or describe it directly.
  value is set; evidence_quote may be null or a supporting verbatim excerpt.
- missing: the paper does not provide it. value MUST be null and evidence_quote null.
- confidence is a number in [0, 1]. human_confirmed MUST be false.
- Do NOT output source_page or source_section keys at all; the system assigns them.
- Never invent numbers, quotes, or results. When unsure, use "missing".
"""


def build_user_prompt(document: ParsedDocument, title_hint: str | None = None) -> str:
    parts: list[str] = []
    if title_hint:
        parts.append(f"Paper title (user-provided hint): {title_hint}")
    if document.title:
        parts.append(f"Detected title: {document.title}")
    if document.authors:
        parts.append(f"Detected authors: {', '.join(document.authors)}")
    if document.ocr_required_pages:
        parts.append(
            "WARNING: pages without a text layer (content unavailable): "
            + ", ".join(str(p) for p in document.ocr_required_pages)
        )
    parts.append("\n--- PAPER TEXT (markers show physical page and section) ---\n")
    for block in document.blocks:
        section = block.section or "-"
        parts.append(f"[p{block.page} | {section}]\n{block.text}\n")
    for table in document.tables:
        rows = "\n".join(" | ".join(row) for row in table.rows)
        parts.append(f"[p{table.page} | TABLE {table.id}]\n{rows}\n")
    parts.append("\n--- END PAPER TEXT ---\nReturn the Reproduction Card JSON now.")
    return "\n".join(parts)
