"""Common scaffolding for OpenAI-VLM-backed PDF parsers.

Provides reusable helpers — PDF rendering, single-page VLM calls under a
semaphore, JSON-fence stripping, picture-bbox cropping — but takes no
opinion on response shape or block layout. Concrete subclasses
implement ``parse()`` and stitch blocks together however suits the
model they target.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC
from io import BytesIO
from typing import Any

from core.indexing.image_preprocessor import pil_to_png_bytes
from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import DocumentType
from core.vlm import VLM

logger = logging.getLogger(__name__)


class BaseOpenAIPdfClient(BaseClientParser, ABC):
    """OpenAI-compatible VLM-backed PDF parser scaffolding."""

    def __init__(
        self,
        vlm: VLM,
        *,
        scale: float = 1.0,
        concurrency_limit: int = 4,
    ) -> None:
        self._vlm = vlm
        self._scale = scale
        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    # ----- helpers -----

    @staticmethod
    def _render_pdf_pages(raw_bytes: bytes, scale: float) -> list[Any]:
        """Render every PDF page into a PIL Image. Pure-CPU; runs in a thread."""
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(raw_bytes)
        try:
            return [page.render(scale=scale).to_pil() for page in pdf]
        finally:
            pdf.close()

    async def _ocr_one(self, page_img: Any, prompt: str) -> str | None:
        """Send one page image through the VLM with ``prompt``; return raw text."""
        async with self._semaphore:
            try:
                png_bytes = pil_to_png_bytes(page_img)
                return await self._vlm.caption_image(png_bytes, prompt=prompt)
            except Exception as exc:
                logger.warning("OpenAI VLM OCR call failed: %s", exc)
                return None

    @staticmethod
    def _strip_json_fences(raw: str) -> str:
        """Strip ```json ... ``` fences and surrounding whitespace from a VLM response."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        return text

    @staticmethod
    def _load_json(raw: str | None) -> Any | None:
        """Decode a JSON payload from a VLM response, tolerating fences and whitespace."""
        if not raw:
            return None
        text = BaseOpenAIPdfClient._strip_json_fences(raw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("OCR response was not valid JSON: %s", exc)
            return None

    @staticmethod
    def _crop_to_png_bytes(page_img: Any, bbox: Any) -> bytes | None:
        """Crop a region from a PIL page image and return PNG bytes."""
        try:
            cropped = page_img.crop(tuple(bbox))
            buf = BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:
            logger.warning("Failed to crop bbox %s: %s", bbox, exc)
            return None
