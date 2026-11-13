"""PPTX ``DocumentParser`` implementation.

Walks slides via ``python-pptx``, converting each slide to Markdown:
title → ``#`` heading, text frames → paragraphs, tables → HTML→Markdown,
charts → Markdown tables, pictures → ``![](pptx-image-N)`` synthetic
markdown image refs. Speaker notes are appended as ``### Notes:``.

Captioning is not done here — see :class:`ImageBlock` for the
parser→caption contract.

Output is one :class:`TextBlock` per slide (1-indexed ``page_number``)
plus one :class:`ImageBlock` per slide picture. ``page_number`` on each
``ImageBlock`` is the slide number it came from.

Implementation derived from the legacy ``PPTXConverter`` (which itself
mirrored the MarkItDown PPTX converter).
"""

from __future__ import annotations

import asyncio
import html
import logging
from io import BytesIO
from typing import Any

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ..image_preprocessor import UPLOADED_IMAGE_FORMATS, ensure_png_compatible_mode, pil_to_png_bytes
from .document_parser import DocumentParser
from .registry import parser_registry

logger = logging.getLogger(__name__)

# Parser-bomb caps (mirrors the EML loader's module-level limits): bound how many
# slides we walk and how many pictures we decode into memory so a crafted PPTX
# can't exhaust CPU/memory during ingestion.
_MAX_SLIDES = 2000
_MAX_IMAGES = 2000


def _image_ref(index: int) -> str:
    """Synthetic markdown image ref used as a placeholder for slide pictures."""
    return f"![](pptx-image-{index})"


@parser_registry.register("pptx")
class PptxParser(DocumentParser):
    """Parse PPTX into one TextBlock per slide plus one ImageBlock per picture."""

    def supported_types(self) -> list[str]:
        return [DocumentType.PPTX.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            slide_count, slides, images = await asyncio.to_thread(self._convert, str(path))

        text_blocks = [TextBlock(text=text, page_number=slide_num) for slide_num, text in slides]
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=slide_count,
        )

    # ----- conversion -----

    def _convert(self, path: str) -> tuple[int, list[tuple[int, str]], list[ImageBlock]]:
        try:
            import pptx
            from PIL import Image
        except ImportError:
            logger.warning("python-pptx or PIL not available; cannot parse PPTX")
            return 0, [], []

        try:
            presentation = pptx.Presentation(path)
        except Exception as exc:
            logger.warning("Failed to open PPTX: %s", exc)
            return 0, [], []

        slides: list[tuple[int, str]] = []
        images: list[ImageBlock] = []

        for slide_num, slide in enumerate(presentation.slides, start=1):
            if slide_num > _MAX_SLIDES:
                logger.warning("Capping PPTX slide processing at %d slides", _MAX_SLIDES)
                break
            md = ""
            try:
                title = slide.shapes.title
                for shape in slide.shapes:
                    # python-pptx reads a shape's parts lazily, so a damaged one
                    # raises only here. Skip that shape, not the whole deck.
                    try:
                        md += self._shape_to_markdown(shape, title, slide_num, images, Image)
                    except Exception as exc:
                        logger.warning("Skipping unreadable PPTX shape: %s", exc)
            except Exception as exc:
                # A shape python-pptx cannot even build, such as a placeholder of
                # an unknown type, raises from slide.shapes itself: keep what was
                # read before it.
                logger.warning("Stopping at an unreadable PPTX shape on slide %d: %s", slide_num, exc)

            md = md.strip()
            try:
                if slide.has_notes_slide:
                    notes_frame = slide.notes_slide.notes_text_frame
                    if notes_frame is not None:
                        md += "\n\n### Notes:\n" + notes_frame.text
            except Exception as exc:
                logger.warning("Skipping unreadable PPTX notes on slide %d: %s", slide_num, exc)
            md = md.strip()

            if md:
                slides.append((slide_num, md))

        return len(presentation.slides), slides, images

    def _shape_to_markdown(
        self, shape: Any, title: Any, slide_num: int, images: list[ImageBlock], pil_image: Any
    ) -> str:
        """One shape's Markdown. A picture is also appended to ``images``."""
        if self._is_picture(shape):
            # Bound pictures decoded into memory across the whole deck.
            if len(images) >= _MAX_IMAGES:
                return ""
            try:
                with pil_image.open(BytesIO(shape.image.blob), formats=UPLOADED_IMAGE_FORMATS) as im:
                    im = ensure_png_compatible_mode(im)
                    png_bytes = pil_to_png_bytes(im)
            except Exception as exc:
                logger.warning("Skipping unreadable PPTX picture: %s", exc)
                return ""
            ref = _image_ref(len(images))
            images.append(
                ImageBlock(
                    image_bytes=png_bytes,
                    page_number=slide_num,
                    mime_type="image/png",
                    metadata={"markdown_ref": ref},
                )
            )
            return ref
        if self._is_table(shape):
            return "\n" + self._table_to_markdown(shape.table) + "\n"
        if getattr(shape, "has_chart", False):
            # Resolving the chart part fails on a missing or dangling r:id, and
            # on one that points at a part that is not a chart.
            try:
                chart = shape.chart
            except Exception as exc:
                logger.warning("Skipping unreadable PPTX chart: %s", exc)
                return "\n\n[unsupported chart]\n\n"
            return self._chart_to_markdown(chart)
        if getattr(shape, "has_text_frame", False):
            if shape == title:
                return "# " + shape.text.lstrip() + "\n"
            return shape.text + "\n"
        return ""

    @staticmethod
    def _is_picture(shape: Any) -> bool:
        try:
            from pptx.enum.shapes import MSO_SHAPE_TYPE

            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                return True
            if shape.shape_type == MSO_SHAPE_TYPE.PLACEHOLDER and hasattr(shape, "image"):
                return True
        except NotImplementedError:
            logger.debug("Encountered an unimplemented shape type")
        except Exception:
            return False
        return False

    @staticmethod
    def _is_table(shape: Any) -> bool:
        try:
            from pptx.enum.shapes import MSO_SHAPE_TYPE

            return shape.shape_type == MSO_SHAPE_TYPE.TABLE
        except NotImplementedError:
            logger.debug("Encountered an unimplemented shape type")
            return False
        except Exception:
            return False

    @staticmethod
    def _table_to_markdown(table: Any) -> str:
        from html_to_markdown import convert

        html_rows = ["<html><body><table>"]
        first_row = True
        for row in table.rows:
            html_rows.append("<tr>")
            for cell in row.cells:
                tag = "th" if first_row else "td"
                html_rows.append(f"<{tag}>{html.escape(cell.text)}</{tag}>")
            html_rows.append("</tr>")
            first_row = False
        html_rows.append("</table></body></html>")
        return convert("".join(html_rows)).strip()

    @staticmethod
    def _chart_to_markdown(chart: Any) -> str:
        try:
            md = "\n\n### Chart"
            if chart.has_title:
                md += f": {chart.chart_title.text_frame.text}"
            md += "\n\n"
            category_names = [c.label for c in chart.plots[0].categories]
            series_names = [s.name for s in chart.series]
            data: list[list[str]] = [["Category"] + series_names]
            for idx, category in enumerate(category_names):
                row = [category]
                for series in chart.series:
                    row.append(series.values[idx])
                data.append(row)
            rows = ["| " + " | ".join(map(str, r)) + " |" for r in data]
            separator = "|" + "|".join(["---"] * len(data[0])) + "|"
            return md + "\n".join([rows[0], separator] + rows[1:])
        except Exception:
            return "\n\n[unsupported chart]\n\n"
