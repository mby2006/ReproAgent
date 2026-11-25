"""Deterministic PDF parsing with PyMuPDF.

Every TextBlock keeps its physical 1-based page number and (heuristic) section.
These page numbers are the single source of truth for evidence grounding;
LLM-claimed pages are never trusted.
"""

import re
from collections import Counter

import pymupdf
from reproagent_schemas import MAX_PDF_BYTES, MAX_PDF_PAGES, ParsedDocument, TableData, TextBlock

from .ocr import OCRProvider

_NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVXLC]{1,5}\.)\s+\S")
_TABLE_CAPTION = re.compile(r"^(table|tab\.)\s", re.IGNORECASE)
_MIN_TEXT_CHARS = 20


def _block_text(block: dict) -> str:
    lines = [
        "".join(span["text"] for span in line["spans"]) for line in block["lines"] if line["spans"]
    ]
    return "\n".join(lines).strip()


def _block_max_size(block: dict) -> float:
    return max(
        (span["size"] for line in block["lines"] for span in line["spans"]),
        default=0.0,
    )


def _body_font_size(text_blocks: list[dict]) -> float:
    weights: Counter[float] = Counter()
    for block in text_blocks:
        for line in block["lines"]:
            for span in line["spans"]:
                weights[round(span["size"], 1)] += len(span["text"])
    if not weights:
        return 10.0
    return weights.most_common(1)[0][0]


def _is_heading(text: str, max_size: float, body_size: float) -> bool:
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) > 150:
        return False
    if _NUMBERED_HEADING.match(first_line) and (
        max_size >= body_size * 1.05 or first_line.isupper()
    ):
        return True
    if max_size >= body_size * 1.18:
        return True
    return first_line.isupper() and len(first_line) <= 60 and max_size >= body_size * 0.95


def _extract_title_authors(
    page_blocks: list[tuple[dict, str]], page_height: float
) -> tuple[str | None, list[str]]:
    """Title = largest font in the top 45% of page 1; authors = name-like lines below it."""
    top = [(b, t) for b, t in page_blocks if b["bbox"][1] < page_height * 0.45]
    if not top:
        return None, []
    title_block, title = max(top, key=lambda bt: _block_max_size(bt[0]))
    title = " ".join(title.split())
    authors: list[str] = []
    seen_title = False
    for block, text in page_blocks:
        if block is title_block:
            seen_title = True
            continue
        if not seen_title:
            continue
        if block["bbox"][1] > page_height * 0.6:
            break
        joined = " ".join(text.split())
        if not joined or "@" in joined or len(joined) > 300:
            continue
        if _NUMBERED_HEADING.match(joined) or joined.lower().startswith("abstract"):
            break
        if re.search(r"\d{3,}", joined):
            continue
        authors.extend(part.strip() for part in re.split(r",| and ", joined) if part.strip())
        if len(authors) >= 8:
            break
    return title, authors[:8]


def parse_pdf(pdf_bytes: bytes, *, ocr: OCRProvider | None = None) -> ParsedDocument:
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_BYTES} byte limit")
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("File is not a PDF")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if doc.page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES} page limit")
        return _parse_document(doc, ocr=ocr)


def _parse_document(doc, *, ocr: OCRProvider | None) -> ParsedDocument:
    blocks: list[TextBlock] = []
    tables: list[TableData] = []
    ocr_required: list[int] = []
    title: str | None = None
    authors: list[str] = []

    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_no = page_index + 1
        raw = page.get_text("dict")
        text_blocks = [b for b in raw["blocks"] if b["type"] == 0 and _block_text(b)]
        body_size = _body_font_size(text_blocks)

        if page_no == 1:
            title, authors = _extract_title_authors(
                [(b, _block_text(b)) for b in text_blocks], page.rect.height
            )

        page_texts: list[str] = []
        section: str | None = None
        for i, block in enumerate(text_blocks):
            text = _block_text(block)
            max_size = _block_max_size(block)
            if _is_heading(text, max_size, body_size):
                section = " ".join(text.split("\n", 1)[0].split())
            blocks.append(
                TextBlock(
                    id=f"p{page_no}-b{i}",
                    text=text,
                    page=page_no,
                    section=section,
                    bbox=list(block["bbox"]),
                )
            )
            page_texts.append(text)

        for t_index, table in enumerate(page.find_tables().tables):
            rows = []
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    if cell is None:
                        cells.append("")
                        continue
                    words = page.get_text("words", clip=pymupdf.Rect(cell))
                    cells.append(" ".join(w[4] for w in words))
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                keep = [c for c in range(width) if any(r[c] for r in rows)]
                rows = [[r[c] for c in keep] for r in rows]
            caption = next(
                (t for t in page_texts if _TABLE_CAPTION.match(t)),
                None,
            )
            tables.append(
                TableData(
                    id=f"p{page_no}-t{t_index}",
                    page=page_no,
                    section=section,
                    caption=" ".join(caption.split()) if caption else None,
                    rows=rows,
                )
            )

        if len(page.get_text().strip()) < _MIN_TEXT_CHARS and page.get_images():
            if ocr is not None:
                png = page.get_pixmap(dpi=200).tobytes("png")
                ocr_text = ocr.extract_page(png, page_no).strip()
                if ocr_text:
                    blocks.append(TextBlock(id=f"p{page_no}-ocr", text=ocr_text, page=page_no))
                    continue
            ocr_required.append(page_no)

    num_pages = doc.page_count
    return ParsedDocument(
        title=title,
        authors=authors,
        num_pages=num_pages,
        blocks=blocks,
        tables=tables,
        ocr_required_pages=ocr_required,
    )
