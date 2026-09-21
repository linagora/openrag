"""Unit tests for :class:`DocParser` (.doc → DocxParser delegation + fallback)."""

from __future__ import annotations

import io
import os
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.indexing.parsers.doc_parser import DocParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock


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
        seen_content = None

        async def capture(doc):
            nonlocal seen_content
            seen_content = pathlib.Path(doc.source_path).read_bytes()
            return expected

        docx_parser.parse = AsyncMock(side_effect=capture)

        parser = DocParser(docx_parser=docx_parser)
        result = await parser.parse(_doc_document())

        assert result is expected
        docx_parser.parse.assert_awaited_once()
        forwarded = docx_parser.parse.await_args.args[0]
        assert forwarded.raw_bytes is None, "the converted .docx was read into memory"
        # Read while the call was live: the file is removed once parse returns.
        assert seen_content == dummy_docx
        assert forwarded.content_type is DocumentType.DOCX
        instance.LoadFromFile.assert_called_once()
        instance.Close.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_forwarded_document_points_at_the_converted_file_not_the_original(self, fake_spire, tmp_path):
        """#911 trap. ``model_copy`` propagates every field, so the derived
        ``.docx`` would carry the original ``.doc``'s ``source_path``. Since
        ``as_temporary_file`` prefers ``source_path`` over ``raw_bytes``,
        ``DocxParser`` would then reopen the *unconverted* ``.doc`` — parsing
        the wrong file, with the conversion silently discarded."""
        src = tmp_path / "legacy.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0fake-doc")

        instance = MagicMock()
        instance.SaveToFile.side_effect = lambda path, _fmt: pathlib.Path(path).write_bytes(b"DOCX-CONTENT")
        fake_spire.return_value = instance

        docx_parser = MagicMock()
        seen_content = None

        async def capture(doc):
            nonlocal seen_content
            seen_content = pathlib.Path(doc.source_path).read_bytes()
            return ProcessedDocument(document_id="test", text_blocks=[], page_count=0)

        docx_parser.parse = AsyncMock(side_effect=capture)

        document = Document(
            filename="legacy.doc",
            content_type=DocumentType.DOC,
            raw_bytes=b"\xd0\xcf\x11\xe0fake-doc",
            source_path=str(src),
        )
        await DocParser(docx_parser=docx_parser).parse(document)

        forwarded = docx_parser.parse.await_args.args[0]
        assert forwarded.source_path != str(src), "the converted .docx still points at the original .doc"
        assert forwarded.source_path.endswith(".docx")
        assert forwarded.raw_bytes is None, "the converted .docx was read into memory anyway"
        assert seen_content == b"DOCX-CONTENT", "guard: the conversion must have produced the .docx"
        # The path the derived document would have to resolve through.
        assert document.source_path == str(src), "guard: the original must keep its own path"

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


class TestConvertedFileIsAPathNotBytes:
    """#846 §3. The converted .docx used to be read whole into memory on top of
    the .doc already there. It is handed over as a path instead."""

    @pytest.mark.asyncio
    async def test_the_converted_file_is_removed_after_the_docx_parser_returns(self, fake_spire, tmp_path):
        """Ownership moved out of ``_convert``: nothing else will clean it up."""
        seen: dict[str, str] = {}
        instance = MagicMock()

        def save_to_file(path: str, _fmt) -> None:
            with open(path, "wb") as fh:
                fh.write(b"DOCX")

        instance.SaveToFile.side_effect = save_to_file
        fake_spire.return_value = instance

        async def capture(doc):
            seen["path"] = doc.source_path
            seen["existed"] = os.path.exists(doc.source_path)
            return ProcessedDocument(document_id="test", text_blocks=[], page_count=0)

        docx_parser = MagicMock()
        docx_parser.parse = AsyncMock(side_effect=capture)

        src = tmp_path / "legacy.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0fake")
        document = Document(
            filename="legacy.doc",
            content_type=DocumentType.DOC,
            raw_bytes=src.read_bytes(),
            source_path=str(src),
        )

        await DocParser(docx_parser=docx_parser).parse(document)

        assert seen["existed"] is True, "the DOCX parser was handed a path that did not exist"
        assert not os.path.exists(seen["path"]), "the converted .docx leaked"

    @pytest.mark.asyncio
    async def test_the_converted_file_is_removed_even_when_the_docx_parser_raises(self, fake_spire, tmp_path):
        instance = MagicMock()
        captured: dict[str, str] = {}

        def save_to_file(path: str, _fmt) -> None:
            captured["path"] = path
            with open(path, "wb") as fh:
                fh.write(b"DOCX")

        instance.SaveToFile.side_effect = save_to_file
        fake_spire.return_value = instance

        docx_parser = MagicMock()
        docx_parser.parse = AsyncMock(side_effect=RuntimeError("boom"))

        src = tmp_path / "legacy.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0fake")
        document = Document(
            filename="legacy.doc",
            content_type=DocumentType.DOC,
            raw_bytes=src.read_bytes(),
            source_path=str(src),
        )

        with pytest.raises(RuntimeError, match="boom"):
            await DocParser(docx_parser=docx_parser).parse(document)

        assert not os.path.exists(captured["path"]), "the converted .docx leaked on the failure path"


class TestAgainstTheRealDocxParser:
    """Every other test here mocks ``DocxParser``, which hid a real break: the
    derived document kept ``filename="legacy.doc"`` while its ``source_path``
    was a ``.docx``, and ``as_temporary_file`` only yields a path whose suffix
    matches the filename's. It rejected the converted file, fell through to
    ``raw_bytes`` (None) and raised — every legacy .doc upload would have
    failed. A mocked collaborator cannot catch a contract between two real ones.
    """

    @pytest.mark.asyncio
    async def test_a_real_docx_parser_can_open_the_handed_over_file(self, fake_spire, tmp_path):
        docx = pytest.importorskip("docx", reason="python-docx is only available transitively")
        from core.indexing.parsers.docx_parser import DocxParser

        built = io.BytesIO()
        d = docx.Document()
        d.add_paragraph("converted content")
        d.save(built)
        payload = built.getvalue()

        instance = MagicMock()
        instance.SaveToFile.side_effect = lambda path, _fmt: pathlib.Path(path).write_bytes(payload)
        fake_spire.return_value = instance

        src = tmp_path / "legacy.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0fake")
        document = Document(
            filename="legacy.doc",
            content_type=DocumentType.DOC,
            raw_bytes=src.read_bytes(),
            source_path=str(src),
        )

        result = await DocParser(docx_parser=DocxParser()).parse(document)

        assert any("converted content" in b.text for b in result.text_blocks), (
            "the real DocxParser produced nothing from the handed-over path"
        )
