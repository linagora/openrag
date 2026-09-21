"""Unit tests for the pymupdf ``DocumentParser`` (issue #640 fallback path)."""

from __future__ import annotations

from unittest.mock import patch

import pymupdf
import pytest
from core.indexing.parsers.pdf.pymupdf import PyMuPDFParser, _extract_markdown, _extract_text
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
            patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4"), [{"text": "ok"}]]),
        ):
            pages, _ = _extract_markdown(raw, "broken.pdf")

        assert pages == ["ok"]
        assert closed_when_second_opened == [True], "the failed document was still open"


class TestOpensThePathWhenThereIsOne:
    """#846 §1a. The shipped default PDF backend used to open a byte stream, so
    the whole document was resident in this process for the length of the parse.
    With a ``source_path`` it opens the file and MuPDF reads it instead."""

    @pytest.mark.asyncio
    async def test_parse_opens_the_path_and_never_touches_the_bytes(self, tmp_path):
        pdf = tmp_path / "saved-upload.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        document = Document(filename="report.pdf", content_type=DocumentType.PDF, source_path=str(pdf))

        result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert document.raw_bytes is None, "a path-opened parse must not materialize the file"

    @pytest.mark.asyncio
    async def test_the_path_wins_when_both_are_present(self, tmp_path):
        """Mutation guard: drop the preference and this reads the bytes instead,
        which is exactly the residency the issue is about."""
        pdf = tmp_path / "on-disk.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        document = Document(
            filename="report.pdf",
            content_type=DocumentType.PDF,
            source_path=str(pdf),
            raw_bytes=b"%PDF-1.7 not the file that should be read",
        )

        opened: list[tuple[tuple, dict]] = []
        real_open = pymupdf.open

        def tracking_open(*args, **kwargs):
            opened.append((args, kwargs))
            return real_open(*args, **kwargs)

        with patch("core.indexing.parsers.pdf.pymupdf.pymupdf.open", side_effect=tracking_open):
            result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert opened[0][0] == (str(pdf),), "opened a stream while a path was available"
        assert "stream" not in opened[0][1]

    @pytest.mark.asyncio
    async def test_bytes_still_parse_when_there_is_no_file(self):
        """An EML attachment is bytes and nothing else — the fallback has to stay."""
        document = Document(filename="attached.pdf", content_type=DocumentType.PDF, raw_bytes=_minimal_pdf_bytes())

        result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]

    @pytest.mark.asyncio
    async def test_neither_source_yields_an_empty_document(self):
        document = Document(filename="empty.pdf", content_type=DocumentType.PDF)

        result = await PyMuPDFParser().parse(document)

        assert result.text_blocks == []

    def test_text_mode_also_opens_the_path(self, tmp_path):
        pdf = tmp_path / "plain.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())

        pages, images = _extract_text(str(pdf), "plain.pdf")

        assert pages == ["hello world"]
        assert images == []

    def test_the_recovery_path_still_works_from_a_path(self, tmp_path):
        """``tobytes`` produces bytes by construction, so the cleaned copy is
        always opened from a stream even when the original came from disk."""
        pdf = tmp_path / "broken.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())

        with patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4"), [{"text": "recovered"}]]) as mock:
            pages, _ = _extract_markdown(str(pdf), "broken.pdf")

        assert pages == ["recovered"]
        assert mock.call_count == 2
