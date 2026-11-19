"""OCR extension point for scanned PDFs.

The parser never silently drops a page: pages without a text layer are either
OCR'd through a provided provider or reported in ParsedDocument.ocr_required_pages.
"""

from abc import ABC, abstractmethod


class OCRProvider(ABC):
    @abstractmethod
    def extract_page(self, page_png: bytes, page_number: int) -> str:
        """Return recognized text for one rendered page (1-based page_number)."""
        ...


class NullOCR(OCRProvider):
    """Default provider: refuses to guess, leaves the page flagged as ocr_required."""

    def extract_page(self, page_png: bytes, page_number: int) -> str:
        return ""
