"""Unit tests for :class:`DoclingParser`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.indexing.parsers.document_parser import BasePooledParser
from core.indexing.parsers.pdf.docling import DoclingParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock


def _make_doc() -> Document:
    return Document(filename="doc.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.4")


class _FakePool(BasePooledParser):
    def __init__(self, result: ProcessedDocument) -> None:
        self._result = result

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        return self._result


class TestDoclingParser:
    def test_rejects_non_pool(self):
        with pytest.raises(ValueError, match="BasePooledParser"):
            DoclingParser(pool=object())  # type: ignore[arg-type]

    def test_supported_types_delegates_to_pool(self):
        pool = _FakePool(ProcessedDocument(document_id="x"))
        assert DoclingParser(pool=pool).supported_types() == [DocumentType.PDF.value]

    @pytest.mark.asyncio
    async def test_parse_delegates_to_pool(self):
        expected = ProcessedDocument(
            document_id="d1",
            text_blocks=[TextBlock(text="hello", page_number=1)],
        )
        pool = _FakePool(expected)
        parser = DoclingParser(pool=pool)
        result = await parser.parse(_make_doc())
        assert result is expected

    @pytest.mark.asyncio
    async def test_parse_propagates_pool_exception(self):
        pool = MagicMock(spec=BasePooledParser)
        pool.supported_types.return_value = [DocumentType.PDF.value]
        pool.parse = AsyncMock(side_effect=RuntimeError("docling crashed"))

        with pytest.raises(RuntimeError, match="docling crashed"):
            await DoclingParser(pool=pool).parse(_make_doc())

    def test_registered_as_docling(self):
        from core.indexing.parsers.registry import parser_registry

        assert "docling" in parser_registry
