"""Unit tests for :class:`DocxParser`."""

from __future__ import annotations

import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ...models.document import Document, DocumentType
from .docx_parser import DocxParser, _image_ref


def _png_bytes(color: str = "red") -> bytes:
    img = Image.new("RGBA", (10, 10), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fake_docx(media_files: dict[str, bytes]) -> Path:
    """Build a minimal .docx zip with given ``word/media/<name>`` entries."""
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    with zipfile.ZipFile(tmp, "w") as zf:
        for name, data in media_files.items():
            zf.writestr(f"word/media/{name}", data)
    return Path(tmp.name)


class TestExtractEmbeddedImages:
    """Mirrors legacy ``TestGetImagesFromZip`` against the new staticmethod."""

    def test_valid_images_kept_in_order(self):
        docx = _fake_docx({"image2.png": _png_bytes("blue"), "image1.png": _png_bytes("red")})
        result = DocxParser._extract_embedded_images(str(docx))
        assert len(result) == 2
        assert result[0] is not None and result[1] is not None

    def test_unsupported_format_collapses_to_none_at_position(self):
        docx = _fake_docx(
            {
                "image1.png": _png_bytes(),
                "image2.emf": b"\x01\x00\x00\x00garbage",
                "image3.png": _png_bytes(),
            }
        )
        result = DocxParser._extract_embedded_images(str(docx))
        assert len(result) == 3
        assert result[0] is not None
        assert result[1] is None
        assert result[2] is not None

    def test_non_image_media_skipped(self):
        docx = _fake_docx(
            {
                "image1.png": _png_bytes(),
                "oleObject1.bin": b"OLE",
                "hdphoto1.wdp": b"WDP",
            }
        )
        result = DocxParser._extract_embedded_images(str(docx))
        assert sum(1 for x in result if x is not None) == 1

    def test_all_unsupported_returns_empty(self):
        docx = _fake_docx({"image1.emf": b"EMF", "image2.wmf": b"WMF"})
        # Two unsupported entries: positional list still has length 2 with None slots.
        result = DocxParser._extract_embedded_images(str(docx))
        assert all(x is None for x in result)

    def test_no_media_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr("word/document.xml", "<doc/>")
        result = DocxParser._extract_embedded_images(tmp.name)
        assert result == []

    def test_invalid_zip_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        tmp.write(b"not a zip")
        tmp.flush()
        assert DocxParser._extract_embedded_images(tmp.name) == []


class TestRewritePlaceholdersAndBuildBlocks:
    """The parser→caption contract: synthetic refs + ImageBlock metadata."""

    def test_assigns_unique_refs_in_order(self):
        md = "before ![](data:image/png;base64,trunc...) middle ![](data:image/png;base64,trunc...) end"
        embedded = [_png_bytes("red"), _png_bytes("blue")]
        new_md, blocks = DocxParser._rewrite_placeholders_and_build_blocks(md, embedded)

        assert _image_ref(0) in new_md
        assert _image_ref(1) in new_md
        assert len(blocks) == 2
        assert blocks[0].metadata["markdown_ref"] == _image_ref(0)
        assert blocks[1].metadata["markdown_ref"] == _image_ref(1)
        assert blocks[0].image_bytes == embedded[0]
        assert blocks[1].image_bytes == embedded[1]
        assert all(b.page_number == 1 and b.mime_type == "image/png" for b in blocks)

    def test_none_entry_drops_placeholder(self):
        md = "![](data:image/png;base64,a) ![](data:image/png;base64,b)"
        embedded = [None, _png_bytes()]
        new_md, blocks = DocxParser._rewrite_placeholders_and_build_blocks(md, embedded)

        # First placeholder collapses to empty; second becomes ref-0 (only one block emitted).
        assert _image_ref(0) in new_md
        assert _image_ref(1) not in new_md
        assert len(blocks) == 1

    def test_more_placeholders_than_zip_entries_drops_extras(self):
        md = "![](data:image/png;base64,a) ![](data:image/png;base64,b)"
        embedded = [_png_bytes()]
        new_md, blocks = DocxParser._rewrite_placeholders_and_build_blocks(md, embedded)

        assert _image_ref(0) in new_md
        # The extra placeholder is dropped — the regex match collapses to "".
        assert "data:image" not in new_md
        assert len(blocks) == 1

    def test_no_placeholders_passthrough(self):
        new_md, blocks = DocxParser._rewrite_placeholders_and_build_blocks("plain text", [_png_bytes()])
        assert new_md == "plain text"
        assert blocks == []

    def test_empty_inputs(self):
        assert DocxParser._rewrite_placeholders_and_build_blocks("", []) == ("", [])
        assert DocxParser._rewrite_placeholders_and_build_blocks("text", []) == ("text", [])


class TestParse:
    @pytest.mark.asyncio
    async def test_empty_raw_bytes_returns_empty(self):
        doc = Document(filename="x.docx", content_type=DocumentType.DOCX, raw_bytes=b"")
        result = await DocxParser().parse(doc)
        assert result.text_blocks == []
        assert result.images == []
        assert result.page_count == 0
