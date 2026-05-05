"""DotsOCR PDF parser — concrete :class:`BaseOpenAIPdfClient` subclass.

DotsOCR returns a JSON list of layout elements (``Picture``, ``Table``,
``Text``, ``Title`` …) with bounding boxes and text content, sorted by
reading order.

Block emission:

- One :class:`TextBlock` per page (1-indexed ``page_number``), holding
  every non-``Picture`` element's text joined in reading order.
- One :class:`ImageBlock` per ``Picture`` element, carrying the cropped
  PNG bytes. Captioning is left to a downstream stage — the parser
  does **not** call the VLM for captions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum

from core.models.document import Document, ImageBlock, ProcessedDocument, TextBlock
from pydantic import BaseModel, RootModel, ValidationError

from ._base_openai_parser import BaseOpenAIPdfClient

logger = logging.getLogger(__name__)


_DOTSOCR_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""


class DotsOCRCategory(str, Enum):
    CAPTION = "Caption"
    FOOTNOTE = "Footnote"
    FORMULA = "Formula"
    LIST_ITEM = "List-item"
    PAGE_FOOTER = "Page-footer"
    PAGE_HEADER = "Page-header"
    PICTURE = "Picture"
    SECTION_HEADER = "Section-header"
    TABLE = "Table"
    TEXT = "Text"
    TITLE = "Title"


class DotsOCRElement(BaseModel):
    """One layout element on a page."""

    bbox: tuple[float, float, float, float]
    category: DotsOCRCategory
    text: str = ""


class DotsOCRPage(RootModel[list[DotsOCRElement]]):
    """One page's DotsOCR output: layout elements in reading order."""

    def pictures(self) -> list[DotsOCRElement]:
        return [e for e in self.root if e.category is DotsOCRCategory.PICTURE]

    def text(self) -> str:
        """Join every non-``Picture`` element's text in reading order."""
        return "\n".join(
            e.text.strip() for e in self.root if e.category is not DotsOCRCategory.PICTURE and e.text and e.text.strip()
        )


class DotsOCRPdfClient(BaseOpenAIPdfClient):
    """OpenAI-compatible PDF parser using the DotsOCR layout-aware prompt."""

    PROMPT: str = _DOTSOCR_PROMPT

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        start = time.time()
        try:
            page_imgs = await asyncio.to_thread(self._render_pdf_pages, document.raw_bytes, self._scale)
            raw_responses = await asyncio.gather(*(self._ocr_one(img, self.PROMPT) for img in page_imgs))
        except Exception:
            logger.exception("DotsOCR PDF parse failed (id=%s)", document.id)
            raise

        text_blocks: list[TextBlock] = []
        images: list[ImageBlock] = []
        for page_number, (page_img, raw) in enumerate(zip(page_imgs, raw_responses, strict=True), start=1):
            page = self._parse_page(raw)
            if page is None:
                continue
            page_text = page.text()
            if page_text:
                text_blocks.append(TextBlock(text=page_text, page_number=page_number))
            for element in page.pictures():
                png = self._crop_to_png_bytes(page_img, element.bbox)
                if png is not None:
                    images.append(ImageBlock(image_bytes=png, page_number=page_number))

        logger.info("DotsOCR PDF parsed (id=%s) in %.2fs", document.id, time.time() - start)

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=len(page_imgs),
        )

    @classmethod
    def _parse_page(cls, raw: str | None) -> DotsOCRPage | None:
        """Validate one page's raw VLM response into a :class:`DotsOCRPage`."""
        payload = cls._load_json(raw)
        if payload is None:
            return None
        # Tolerate ``{"items": [...]}`` envelope as well as a bare list.
        if isinstance(payload, dict) and "items" in payload:
            payload = payload["items"]
        try:
            return DotsOCRPage.model_validate(payload)
        except ValidationError as exc:
            logger.warning("DotsOCR response did not match expected schema: %s", exc)
            return None
