"""Active understanding and deterministic planning graph nodes."""

import asyncio
import base64
import binascii

from reproagent_paperparse import parse_pdf

from ..llm.base import LLMAdapter
from ..planning import build_plan
from ..understanding import extract_card
from .state import AgentState


async def understand_paper(state: AgentState) -> dict:
    pdf_b64 = state.get("pdf_b64")
    if not pdf_b64:
        raise ValueError("understand_paper requires pdf_b64 in state")
    try:
        pdf_bytes = base64.b64decode(pdf_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 PDF payload: {exc}") from exc
    try:
        document = await asyncio.to_thread(parse_pdf, pdf_bytes)
    except Exception as exc:
        raise ValueError(f"PDF parsing failed: {exc}") from exc
    return {"document": document}


def make_extract_spec(llm: LLMAdapter):
    async def extract_spec(state: AgentState) -> dict:
        document = state.get("document")
        if document is None:
            raise ValueError("extract_spec requires a parsed document in state")
        card = await extract_card(llm, document, state.get("paper_title") or None)
        return {"card": card}

    return extract_spec


async def plan_experiment(state: AgentState) -> dict:
    card = state.get("card")
    if card is None:
        raise ValueError("plan_experiment requires a ReproductionCard in state")
    return {"plan": build_plan(card, state.get("scaling"))}
