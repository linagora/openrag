"""Unit tests for the pymupdf ``DocumentParser`` (issue #640 and #1102 fallback paths)."""

from __future__ import annotations

from unittest.mock import patch

import pymupdf
import pytest
from core.indexing.parsers.pdf.pymupdf import (
    _MARKDOWN_TEXT_RATIO_THRESHOLD,
    PyMuPDFParser,
    _extract_markdown,
    _pages_with_fallback,
)
from core.models.document import Document, DocumentType

_TO_MARKDOWN = "core.indexing.parsers.pdf.pymupdf.pymupdf4llm.to_markdown"


def _minimal_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "hello world")
        return doc.tobytes()
    finally:
        doc.close()


class TestExtractMarkdownFallback:
    def test_retries_once_against_cleaned_copy_on_runtime_error(self):
        raw = _minimal_pdf_bytes()
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4: no font file for digest"), good_chunks]) as mock:
            pages, images = _extract_markdown(raw, "broken.pdf")

        assert pages == ["hello world"]
        assert images == []
        assert mock.call_count == 2

    def test_only_retries_once_and_propagates_if_still_failing(self):
        raw = _minimal_pdf_bytes()

        with patch(_TO_MARKDOWN, side_effect=RuntimeError("still broken")) as mock:
            with pytest.raises(RuntimeError, match="still broken"):
                _extract_markdown(raw, "broken.pdf")

        assert mock.call_count == 2

    def test_no_retry_when_first_attempt_succeeds(self):
        raw = _minimal_pdf_bytes()
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, return_value=good_chunks) as mock:
            pages, _ = _extract_markdown(raw, "clean.pdf")

        assert pages == ["hello world"]
        assert mock.call_count == 1


class TestPyMuPDFParserRecovery:
    @pytest.mark.asyncio
    async def test_parse_recovers_from_transient_runtime_error(self):
        raw = _minimal_pdf_bytes()
        document = Document(filename="broken.pdf", content_type=DocumentType.PDF, raw_bytes=raw)
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4: no font file for digest"), good_chunks]):
            result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert result.page_count == 1


class TestRecoveryPathResidency:
    def test_failed_document_is_closed_before_the_cleaned_copy_is_opened(self):
        """Peak memory on the #640 recovery path (#846).

        The failed document and the cleaned copy used to be open at the same
        time, so MuPDF held parsed structures for both. Closing the first before
        opening the second releases one of them. ``raw`` itself belongs to the
        caller's Document and stays live either way — this is the part the
        parser can actually control.
        """
        raw = _minimal_pdf_bytes()
        opened: list[pymupdf.Document] = []
        real_open = pymupdf.open
        closed_when_second_opened: list[bool] = []

        def tracking_open(*args, **kwargs):
            if opened:
                closed_when_second_opened.append(opened[0].is_closed)
            doc = real_open(*args, **kwargs)
            opened.append(doc)
            return doc

        with (
            patch("core.indexing.parsers.pdf.pymupdf.pymupdf.open", side_effect=tracking_open),
            patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4"), [{"text": "hello world"}]]),
        ):
            pages, _ = _extract_markdown(raw, "broken.pdf")

        assert pages == ["hello world"]
        assert closed_when_second_opened == [True], "the failed document was still open"


class TestPagesWithFallback:
    """Per-page plain-text fallback when pymupdf4llm drops text (issue #1102)."""

    def _pdf_with_text(self, text: str) -> bytes:
        doc = pymupdf.open()
        try:
            page = doc.new_page()
            page.insert_text((72, 72), text)
            return doc.tobytes()
        finally:
            doc.close()

    def test_keeps_markdown_when_text_is_retained(self):
        """No fallback when markdown length is at least the threshold fraction."""
        raw = self._pdf_with_text("hello world")
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            chunks = [{"text": "hello world"}]
            pages = _pages_with_fallback(doc, chunks, "ok.pdf")
        assert pages == ["hello world"]

    def test_falls_back_to_plain_text_when_markdown_is_nearly_empty(self):
        """Markdown returning almost no text is replaced with page.get_text()."""
        raw = self._pdf_with_text("hello world this is a longer sentence to exceed the threshold")
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            plain_text = doc[0].get_text().strip()
            # Simulate pymupdf4llm returning nearly nothing
            chunks = [{"text": "x"}]
            pages = _pages_with_fallback(doc, chunks, "indesign.pdf")
        assert pages == [plain_text]

    def test_no_fallback_when_plain_text_is_empty(self):
        """Pages with no text layer at all (e.g. scanned image pages) keep the empty markdown result."""
        doc = pymupdf.open()
        try:
            doc.new_page()
            raw = doc.tobytes()
        finally:
            doc.close()

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            chunks = [{"text": ""}]
            pages = _pages_with_fallback(doc, chunks, "scanned.pdf")
        assert pages == [""]

    def test_mixed_pages_fallback_only_on_affected_pages(self):
        """Only the pages that drop below the threshold are replaced; good pages keep their markdown."""
        pdf_doc = pymupdf.open()
        try:
            page0 = pdf_doc.new_page()
            page0.insert_text((72, 72), "hello world this is a long sentence")
            page1 = pdf_doc.new_page()
            page1.insert_text((72, 72), "another page with text")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            plain0 = doc[0].get_text().strip()
            good_md1 = "another page with text"
            # Page 0 gets almost no markdown, page 1 gets full markdown
            chunks = [{"text": "z"}, {"text": good_md1}]
            pages = _pages_with_fallback(doc, chunks, "mixed.pdf")

        assert pages[0] == plain0
        assert pages[1] == good_md1

    def test_threshold_constant_is_below_one(self):
        """Sanity check: the threshold must be a fraction in (0, 1)."""
        assert 0 < _MARKDOWN_TEXT_RATIO_THRESHOLD < 1


class TestExtractMarkdownPerPageFallback:
    """Integration: _extract_markdown uses per-page fallback via _pages_with_fallback."""

    def test_falls_back_per_page_without_raising(self):
        """When pymupdf4llm silently drops text, _extract_markdown recovers it."""
        pdf_doc = pymupdf.open()
        try:
            page = pdf_doc.new_page()
            page.insert_text((72, 72), "important document content that must not be lost")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        # Simulate pymupdf4llm returning nearly empty text for the page
        empty_chunks = [{"text": ""}]
        with patch(_TO_MARKDOWN, return_value=empty_chunks):
            pages, images = _extract_markdown(raw, "indesign_cs3.pdf")

        assert images == []
        assert len(pages) == 1
        # The fallback should have recovered the text layer content
        assert len(pages[0]) > 5

    @pytest.mark.asyncio
    async def test_parser_does_not_return_empty_blocks_when_text_layer_has_content(self):
        """End-to-end: PyMuPDFParser recovers non-empty text when pymupdf4llm drops it."""
        pdf_doc = pymupdf.open()
        try:
            page = pdf_doc.new_page()
            page.insert_text((72, 72), "important content")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        document = Document(filename="lossy.pdf", content_type=DocumentType.PDF, raw_bytes=raw)
        empty_chunks = [{"text": ""}]
        with patch(_TO_MARKDOWN, return_value=empty_chunks):
            result = await PyMuPDFParser().parse(document)

        assert result.page_count == 1
        assert result.text_blocks[0].text != ""
