import asyncio
import base64
import binascii

from fastapi import APIRouter, HTTPException, Request, status
from reproagent_paperparse import parse_pdf
from reproagent_schemas import (
    ExtractRequest,
    ExtractResponse,
    GroundRequest,
    GroundResponse,
    ParseRequest,
    ParseResponse,
    UnderstandRequest,
    UnderstandResponse,
)

from ..understanding import UnderstandingError, extract_raw, ground_and_validate

router = APIRouter(prefix="/v1", tags=["understanding"])


def _decode_pdf(pdf_b64: str) -> bytes:
    try:
        return base64.b64decode(pdf_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid PDF base64: {exc}"
        ) from exc


@router.post("/parse", response_model=ParseResponse)
async def parse_paper(body: ParseRequest) -> ParseResponse:
    pdf_bytes = _decode_pdf(body.pdf_b64)
    try:
        document = await asyncio.to_thread(parse_pdf, pdf_bytes)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"PDF parsing failed: {exc}"
        ) from exc
    return ParseResponse(document=document)


@router.post("/extract", response_model=ExtractResponse)
async def extract_spec(body: ExtractRequest, request: Request) -> ExtractResponse:
    llm = request.app.state.llm
    try:
        raw, usages = await extract_raw(llm, body.document, body.title_hint)
    except UnderstandingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return ExtractResponse(card_raw=raw, usages=usages)


@router.post("/ground", response_model=GroundResponse)
async def ground_spec(body: GroundRequest) -> GroundResponse:
    try:
        card = ground_and_validate(body.card_raw, body.document)
    except UnderstandingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return GroundResponse(card=card)


@router.post("/understand", response_model=UnderstandResponse)
async def understand_paper(body: UnderstandRequest, request: Request) -> UnderstandResponse:
    _decode_pdf(body.pdf_b64)
    graph = request.app.state.understanding_graph
    try:
        result = await graph.ainvoke(
            {
                "job_id": str(body.paper_id),
                "paper_title": body.title_hint or "",
                "pdf_b64": body.pdf_b64,
            }
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except UnderstandingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return UnderstandResponse(
        paper_id=body.paper_id, document=result["document"], card=result["card"]
    )
