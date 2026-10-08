"""#911 — ``Document.source_path``.

Pooled parsers (Marker, Docling, Whisper) hand a filesystem path to worker
actors that may live on another Ray node. When that path came from
``NamedTemporaryFile`` it was visible only to the node that wrote it, so the
parser tier could not be scaled horizontally. These tests pin the contract that
makes the path placeable — and, above all, that yielding the caller's own file
never deletes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.models.document import Document, DocumentType


def _upload(tmp_path: Path, name: str = "report.pdf", data: bytes = b"%PDF-1.4 real upload") -> Path:
    src = tmp_path / name
    src.write_bytes(data)
    return src


class TestSourcePathIsYieldedAsIs:
    @pytest.mark.asyncio
    async def test_yields_the_source_path_itself_not_a_copy(self, tmp_path: Path):
        """The whole point: a worker on another node must receive the shared
        path, so the yielded path has to be that file, not a local duplicate."""
        src = _upload(tmp_path)
        doc = Document(filename="report.pdf", content_type=DocumentType.PDF, source_path=str(src))

        async with doc.as_temporary_file() as path:
            assert path == src

    @pytest.mark.asyncio
    async def test_the_source_file_is_never_deleted(self, tmp_path: Path):
        """``as_temporary_file`` unlinks what it writes. Handing it the real
        upload must not put that file on the deletion path — the upload is
        ``indexer_pool``'s to purge after indexing settles, not this helper's."""
        src = _upload(tmp_path)

        doc = Document(filename="report.pdf", content_type=DocumentType.PDF, source_path=str(src))
        async with doc.as_temporary_file() as path:
            assert path.read_bytes() == b"%PDF-1.4 real upload"

        assert src.exists(), "the upload was deleted by as_temporary_file"
        assert src.read_bytes() == b"%PDF-1.4 real upload", "the upload was truncated or rewritten"

    @pytest.mark.asyncio
    async def test_the_source_file_survives_a_failing_parse(self, tmp_path: Path):
        """The cleanup that must not happen lives in a ``finally``, so the
        exception path is where an unconditional unlink would show up."""
        src = _upload(tmp_path)
        doc = Document(filename="report.pdf", content_type=DocumentType.PDF, source_path=str(src))

        with pytest.raises(RuntimeError, match="parser exploded"):
            async with doc.as_temporary_file():
                raise RuntimeError("parser exploded")

        assert src.exists(), "a failing parse deleted the upload"

    @pytest.mark.asyncio
    async def test_source_path_wins_over_raw_bytes(self, tmp_path: Path):
        """Both are set on the indexing path today. The path must be preferred,
        or the temp-file write this exists to remove would still happen."""
        src = _upload(tmp_path, data=b"on-disk")
        doc = Document(
            filename="report.pdf",
            content_type=DocumentType.PDF,
            raw_bytes=b"in-memory",
            source_path=str(src),
        )

        async with doc.as_temporary_file() as path:
            assert path == src
            assert path.read_bytes() == b"on-disk"


class TestDerivedDocumentsStillGetATempFile:
    @pytest.mark.asyncio
    async def test_bytes_only_document_gets_a_temp_file_that_is_cleaned_up(self):
        """Derived documents — the converted ``.docx``, EML attachments — have no
        file of their own, so the old behaviour must survive untouched."""
        doc = Document(filename="converted.docx", content_type=DocumentType.DOCX, raw_bytes=b"PK\x03\x04data")

        async with doc.as_temporary_file() as path:
            captured = path
            assert path.exists()
            assert path.read_bytes() == b"PK\x03\x04data"
            assert path.suffix == ".docx"

        assert not captured.exists(), "the temp file leaked"

    @pytest.mark.asyncio
    async def test_temp_file_is_cleaned_up_when_the_body_raises(self):
        doc = Document(filename="converted.docx", content_type=DocumentType.DOCX, raw_bytes=b"PK\x03\x04data")

        captured: Path | None = None
        with pytest.raises(RuntimeError, match="boom"):
            async with doc.as_temporary_file() as path:
                captured = path
                raise RuntimeError("boom")

        assert captured is not None and not captured.exists()

    @pytest.mark.asyncio
    async def test_requires_raw_bytes_or_source_path(self):
        doc = Document(filename="nothing.pdf", content_type=DocumentType.PDF)

        with pytest.raises(ValueError, match="raw_bytes or source_path"):
            async with doc.as_temporary_file():
                pass


class TestSuffixMismatchFallsBack:
    @pytest.mark.asyncio
    async def test_a_source_path_with_the_wrong_extension_is_not_yielded(self, tmp_path: Path):
        """Marker, Whisper and MarkItDown dispatch on the extension. An upload
        stored without one (or under a different one) must not be handed over as
        if it were a ``.pdf`` — fall back to writing the bytes under the
        requested suffix instead."""
        src = _upload(tmp_path, name="upload-without-extension", data=b"on-disk")
        doc = Document(
            filename="report.pdf",
            content_type=DocumentType.PDF,
            raw_bytes=b"in-memory",
            source_path=str(src),
        )

        async with doc.as_temporary_file() as path:
            assert path != src
            assert path.suffix == ".pdf"
            assert path.read_bytes() == b"in-memory"

        assert src.exists(), "the fallback deleted the source file"
