"""Unit tests for :class:`DocParser` (.doc → DocxParser delegation + fallback)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ...models.document import Document, DocumentType, ProcessedDocument, TextBlock
from .doc_parser import DocParser


@pytest.fixture
def fake_spire():
    """Inject a fake ``spire.doc`` into ``sys.modules`` for the duration of a test.

    Returns the ``Document`` mock class so tests can configure the
    instance returned by ``Document()``.
    """
    spire = MagicMock()
    spire_doc = MagicMock()
    spire.doc = spire_doc
    saved = {k: sys.modules.get(k) for k in ("spire", "spire.doc")}
    sys.modules["spire"] = spire
    sys.modules["spire.doc"] = spire_doc
    try:
        yield spire_doc.Document
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _doc_document(raw: bytes = b"\xd0\xcf\x11\xe0fake-doc") -> Document:
    return Document(filename="x.doc", content_type=DocumentType.DOC, raw_bytes=raw)


class TestParse:
    @pytest.mark.asyncio
    async def test_empty_raw_bytes_returns_empty(self):
        doc = _doc_document(raw=b"")
        result = await DocParser().parse(doc)
        assert result.text_blocks == [] and result.images == [] and result.page_count == 0

    @pytest.mark.asyncio
    async def test_successful_conversion_delegates_to_docx(self, fake_spire, tmp_path):
        # Spire writes a real .docx file at the path given to SaveToFile.
        dummy_docx = b"DOCX-CONTENT"

        instance = MagicMock()

        def save_to_file(path: str, _fmt) -> None:
            with open(path, "wb") as fh:
                fh.write(dummy_docx)

        instance.SaveToFile.side_effect = save_to_file
        fake_spire.return_value = instance

        docx_parser = MagicMock()
        expected = ProcessedDocument(
            document_id="test",
            text_blocks=[TextBlock(text="from-docx", page_number=1)],
            page_count=1,
        )
        docx_parser.parse = AsyncMock(return_value=expected)

        parser = DocParser(docx_parser=docx_parser)
        result = await parser.parse(_doc_document())

        assert result is expected
        docx_parser.parse.assert_awaited_once()
        forwarded = docx_parser.parse.await_args.args[0]
        assert forwarded.raw_bytes == dummy_docx
        assert forwarded.content_type is DocumentType.DOCX
        instance.LoadFromFile.assert_called_once()
        instance.Close.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_failure_falls_back_to_get_text(self, fake_spire):
        instance = MagicMock()
        instance.SaveToFile.side_effect = RuntimeError("Spire crashed")
        instance.GetText.return_value = "  plain text content  "
        fake_spire.return_value = instance

        docx_parser = MagicMock()
        docx_parser.parse = AsyncMock()
        result = await DocParser(docx_parser=docx_parser).parse(_doc_document())

        assert result.text_blocks == [TextBlock(text="plain text content", page_number=1)]
        assert result.page_count == 1
        instance.GetText.assert_called_once()
        instance.Close.assert_called_once()
        docx_parser.parse.assert_not_awaited()  # never delegated

    @pytest.mark.asyncio
    async def test_total_failure_returns_empty(self, fake_spire):
        instance = MagicMock()
        instance.SaveToFile.side_effect = RuntimeError("Spire crashed")
        instance.GetText.side_effect = RuntimeError("GetText crashed")
        fake_spire.return_value = instance

        result = await DocParser().parse(_doc_document())
        assert result.text_blocks == [] and result.page_count == 0
        instance.Close.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_spire_returns_empty(self, monkeypatch):
        # spire-doc is in the runtime deps, so just omitting fake_spire would
        # actually drive a real Spire instance against malformed bytes.
        # Pin the import to None so ``_convert``'s ``import spire.doc`` raises
        # ImportError deterministically.
        monkeypatch.setitem(sys.modules, "spire", None)
        monkeypatch.setitem(sys.modules, "spire.doc", None)
        result = await DocParser().parse(_doc_document())
        assert result.text_blocks == [] and result.page_count == 0
