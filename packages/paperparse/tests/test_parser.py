import pytest
from reproagent_paperparse import OCRProvider, parse_pdf, parser


class TestParsePdf:
    def test_page_count_and_ids(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        assert doc.num_pages == 3
        ids = [b.id for b in doc.blocks]
        assert len(ids) == len(set(ids))
        assert all(1 <= b.page <= 3 for b in doc.blocks)

    def test_title_and_authors(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        assert doc.title == "Gradient-Based Learning Applied to Document Recognition"
        assert "Yann LeCun" in doc.authors
        assert len(doc.authors) == 4

    def test_page_numbers_preserved(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        c3 = next(b for b in doc.blocks if "Layer C3" in b.text)
        assert c3.page == 2
        abstract = next(b for b in doc.blocks if "We present LeNet-5" in b.text)
        assert abstract.page == 1

    def test_section_assignment(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        c3 = next(b for b in doc.blocks if "Layer C3" in b.text)
        assert c3.section == "II. THE LENET-5 ARCHITECTURE"
        results = next(b for b in doc.blocks if "error rate on the MNIST" in b.text)
        assert results.section == "III. RESULTS AND COMPARISONS"

    def test_table_extraction(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        assert doc.tables, "expected at least one table on page 3"
        table = doc.tables[0]
        assert table.page == 3
        flat = [cell for row in table.rows for cell in row]
        assert "LeNet-5" in flat
        assert "0.8" in flat

    def test_bbox_recorded(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        assert all(b.bbox is not None and len(b.bbox) == 4 for b in doc.blocks)

    def test_byte_limit_rejected_before_parser(self, monkeypatch):
        monkeypatch.setattr(parser, "MAX_PDF_BYTES", 8)
        with pytest.raises(ValueError, match="50 MB|byte limit"):
            parse_pdf(b"%PDF-1.4 fake body")

    def test_page_limit_rejected_before_text_extraction(self, paper_pdf, monkeypatch):
        monkeypatch.setattr(parser, "MAX_PDF_PAGES", 2)
        with pytest.raises(ValueError, match="200 pages|page limit"):
            parse_pdf(paper_pdf)

    def test_document_closes_when_extraction_fails(self, paper_pdf, monkeypatch):
        documents = []

        def fail_extraction(doc, *, ocr):
            documents.append(doc)
            raise RuntimeError("extraction failed")

        monkeypatch.setattr(parser, "_parse_document", fail_extraction)
        with pytest.raises(RuntimeError, match="extraction failed"):
            parse_pdf(paper_pdf)
        assert len(documents) == 1
        assert documents[0].is_closed


class TestOcr:
    def test_scanned_page_flagged_not_dropped(self, scanned_pdf):
        doc = parse_pdf(scanned_pdf)
        assert doc.ocr_required_pages == [1]
        assert doc.num_pages == 1

    def test_ocr_provider_recovers_text(self, scanned_pdf):
        class FakeOCR(OCRProvider):
            def extract_page(self, page_png: bytes, page_number: int) -> str:
                assert page_png.startswith(b"\x89PNG")
                assert page_number == 1
                return "Recovered scanned text about LeNet-5."

        doc = parse_pdf(scanned_pdf, ocr=FakeOCR())
        assert doc.ocr_required_pages == []
        assert any("Recovered scanned text" in b.text and b.page == 1 for b in doc.blocks)

    def test_empty_ocr_result_still_flagged(self, scanned_pdf):
        class EmptyOCR(OCRProvider):
            def extract_page(self, page_png: bytes, page_number: int) -> str:
                return ""

        doc = parse_pdf(scanned_pdf, ocr=EmptyOCR())
        assert doc.ocr_required_pages == [1]
