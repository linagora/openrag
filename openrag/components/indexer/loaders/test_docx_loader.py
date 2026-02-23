"""
Unit tests for DocxLoader.get_images_from_zip image extraction.

Tests validate that unsupported media formats (EMF, WMF, OLE objects, etc.)
are gracefully skipped instead of crashing the entire DOCX ingestion pipeline.
"""

import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from .docx import DocxLoader


def _create_png_bytes(width=10, height=10, color="red"):
    """Create minimal PNG image bytes."""
    img = Image.new("RGBA", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _create_fake_docx(media_files: dict[str, bytes]) -> Path:
    """Create a minimal .docx (zip) with given word/media/ entries.

    Args:
        media_files: mapping of filename (e.g. "image1.png") to raw bytes.

    Returns:
        Path to the temporary .docx file.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    with zipfile.ZipFile(tmp, "w") as zf:
        for name, data in media_files.items():
            zf.writestr(f"word/media/{name}", data)
    return Path(tmp.name)


class TestGetImagesFromZip:
    """Test DocxLoader.get_images_from_zip with various media contents."""

    def _make_loader(self):
        """Create a DocxLoader without Hydra config (only get_images_from_zip is used)."""
        loader = object.__new__(DocxLoader)
        return loader

    def test_valid_images_returned_in_order(self):
        """Valid PNG images are extracted and reordered by their number."""
        red = _create_png_bytes(color="red")
        blue = _create_png_bytes(color="blue")
        # Insert out of order: image2 before image1
        docx_path = _create_fake_docx({"image2.png": blue, "image1.png": red})

        loader = self._make_loader()
        images = loader.get_images_from_zip(docx_path)

        assert len(images) == 2
        # image1 (red) should come first
        assert images[0] is not None
        assert images[1] is not None

    def test_unsupported_format_skipped(self):
        """Unsupported formats (EMF, WMF, etc.) are skipped, valid images preserved."""
        valid_png = _create_png_bytes()
        fake_emf = b"\x01\x00\x00\x00EMF_GARBAGE_DATA"

        docx_path = _create_fake_docx(
            {
                "image1.png": valid_png,
                "image2.emf": fake_emf,
                "image3.png": valid_png,
            }
        )

        loader = self._make_loader()
        images = loader.get_images_from_zip(docx_path)

        # image2.emf is skipped; images list sized to max order (3)
        # with None at position 2 (index 1) for the skipped EMF
        assert len(images) == 3
        assert images[0] is not None  # image1.png
        assert images[1] is None  # image2.emf was skipped
        assert images[2] is not None  # image3.png

    def test_non_image_media_files_skipped(self):
        """Files like oleObject1.bin are skipped via try/except (not valid images)."""
        valid_png = _create_png_bytes()
        docx_path = _create_fake_docx(
            {
                "image1.png": valid_png,
                "oleObject1.bin": b"OLE_DATA",
                "hdphoto1.wdp": b"WDP_DATA",
            }
        )

        loader = self._make_loader()
        images = loader.get_images_from_zip(docx_path)

        # oleObject1.bin and hdphoto1.wdp fail Image.open() or order parsing
        # Only image1.png succeeds
        assert sum(1 for img in images if img is not None) == 1

    def test_all_unsupported_returns_empty(self):
        """When all image files are unsupported, returns empty list."""
        docx_path = _create_fake_docx(
            {
                "image1.emf": b"EMF_DATA",
                "image2.wmf": b"WMF_DATA",
            }
        )

        loader = self._make_loader()
        images = loader.get_images_from_zip(docx_path)

        assert images == []

    def test_no_media_returns_empty(self):
        """DOCX with no word/media/ files returns empty list."""
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr("word/document.xml", "<doc/>")

        loader = self._make_loader()
        images = loader.get_images_from_zip(Path(tmp.name))

        assert images == []
