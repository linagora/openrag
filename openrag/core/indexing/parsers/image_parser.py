"""Image ``DocumentParser`` implementation.

Decodes an image into normalized PNG bytes and emits a single
:class:`ImageBlock`. Supports the raster formats in
``UPLOADED_IMAGE_FORMATS`` (PNG, JPEG, GIF, WebP, BMP) and SVG (rasterized
to PNG via cairosvg, with embedded rasters held to the same formats).

Captioning is not done here — see :class:`ImageBlock` for the
parser→caption contract.

Output:
- A single ``ImageBlock`` with the normalized PNG bytes and no caption.
- No ``TextBlock`` is emitted; downstream stages produce text from the image.

Failures (decode errors, undersized images) emit an empty
``ProcessedDocument`` rather than raising — RAG pipelines should not die
on a single bad image.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument
from ..image_preprocessor import (
    MIN_IMAGE_PIXELS,
    UPLOADED_IMAGE_FORMATS,
    ensure_png_compatible_mode,
    pil_to_png_bytes,
)
from .document_parser import DocumentParser
from .registry import parser_registry

logger = logging.getLogger(__name__)

# Payloads cairosvg's ``<image>`` renders without Pillow: its own PNG loader, or
# a nested SVG tree (``cairosvg/image.py``). Everything else it hands to a plain
# ``Image.open``.
_CAIROSVG_NATIVE_PREFIXES = (b"\x89PNG", b"<svg ", b"<?xml", b"<!DOC", b"\x1f\x8b")
_EMPTY_SVG = b'<svg width="1" height="1"></svg>'


def _fetch_svg_resource(url: str, resource_type: str) -> bytes:
    """cairosvg ``url_fetcher`` that decodes embedded rasters only in ``UPLOADED_IMAGE_FORMATS``.

    ``safe_fetch`` still serves ``data:`` URLs, and cairosvg would open an
    ``<image>`` payload in any format Pillow knows. Such payloads are re-encoded
    as PNG here, or replaced by an empty SVG when their format is not allowed.
    The EXIF orientation is applied first, as cairosvg does before its own save.
    Nested SVGs inherit this fetcher.
    """
    from cairosvg.url import safe_fetch
    from PIL import Image, ImageOps

    data = safe_fetch(url, resource_type)
    if resource_type != "image/*" or data.startswith(_CAIROSVG_NATIVE_PREFIXES) or b"<svg" in data:
        return data
    try:
        with Image.open(BytesIO(data), formats=UPLOADED_IMAGE_FORMATS) as image:
            return pil_to_png_bytes(ImageOps.exif_transpose(image))
    except Exception as exc:
        logger.warning("Skipping image embedded in SVG: %s", exc)
        return _EMPTY_SVG


@parser_registry.register("image")
class ImageParser(DocumentParser):
    """Decode an image and emit it as a single ``ImageBlock``."""

    def __init__(self, *, min_pixels: int = MIN_IMAGE_PIXELS) -> None:
        self._min_pixels = max(0, min_pixels)

    def supported_types(self) -> list[str]:
        return [DocumentType.IMAGE.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        png_bytes = await asyncio.to_thread(self._normalize_to_png, document)
        if png_bytes is None:
            logger.warning("ImageParser: failed to decode image (id=%s)", document.id)
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        if await asyncio.to_thread(self._below_min_pixels, png_bytes):
            logger.warning("ImageParser: image below min_pixels threshold (id=%s)", document.id)
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        return ProcessedDocument(
            document_id=document.id,
            images=[
                ImageBlock(
                    image_bytes=png_bytes,
                    page_number=1,
                    mime_type="image/png",
                )
            ],
            metadata=dict(document.metadata),
            page_count=1,
        )

    def _normalize_to_png(self, document: Document) -> bytes | None:
        """Return PNG bytes for any supported image input, or None on failure."""
        raw = document.raw_bytes
        if not raw:
            return None
        if self._is_svg(raw, document.filename):
            return self._svg_to_png(raw)
        return self._raster_to_png(raw)

    @staticmethod
    def _is_svg(raw: bytes, filename: str) -> bool:
        if filename.lower().endswith(".svg"):
            return True
        head = raw[:200].lstrip().lower()
        return head.startswith((b"<?xml", b"<svg"))

    @staticmethod
    def _svg_to_png(raw: bytes) -> bytes | None:
        try:
            from cairosvg.surface import PNGSurface

            # unsafe=False (cairosvg's default) sets forbid_external/forbid_entities,
            # so an untrusted SVG cannot expand XML entities (XXE). The fetcher wraps
            # safe_fetch, so it cannot pull external URLs (SSRF) either. svg2png takes
            # no url_fetcher, PNGSurface.convert passes it to the tree.
            return PNGSurface.convert(bytestring=raw, unsafe=False, url_fetcher=_fetch_svg_resource)
        except Exception as exc:
            logger.warning("Failed to rasterize SVG: %s", exc)
            return None

    @staticmethod
    def _raster_to_png(raw: bytes) -> bytes | None:
        """Decode raw bytes through PIL and re-encode as PNG.

        Re-encoding normalizes the format so downstream consumers
        (caption stage, vector-store image fields) only need to handle
        one mime type, and validates the image is decodable.
        """
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError:
            logger.warning("PIL not available; cannot decode raster image")
            return None
        try:
            with Image.open(BytesIO(raw), formats=UPLOADED_IMAGE_FORMATS) as image:
                image = ensure_png_compatible_mode(image)
                buf = BytesIO()
                image.save(buf, format="PNG")
                return buf.getvalue()
        except Exception as exc:
            logger.warning("Failed to decode image: %s", exc)
            return None

    def _below_min_pixels(self, png_bytes: bytes) -> bool:
        if self._min_pixels <= 0:
            return False
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError:
            return False
        try:
            with Image.open(BytesIO(png_bytes)) as image:
                return image.width * image.height < self._min_pixels
        except Exception:
            return False
