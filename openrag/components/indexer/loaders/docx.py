"""
DOCX file loader implementation.

``DocxLoader`` is now a thin :class:`BaseLoader` adapter that delegates
extraction to :class:`core.indexing.parsers.docx_parser.DocxParser` and
layers VLM captioning of embedded images on top via the ``BaseLoader``
mixin. The legacy ``convert_to_png_image`` helper and the
``get_images_from_zip`` instance method are preserved for backward
compatibility with existing test consumers; new code should use the
core parser directly.
"""

import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.docx_parser import DocxParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from PIL import Image
from utils.logger import get_logger

from .base import BaseLoader, ensure_png_compatible_mode

logger = get_logger()


def convert_to_png_image(image: Image.Image) -> Image.Image:
    image = ensure_png_compatible_mode(image)
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        buffer.seek(0)
        png_image = Image.open(buffer).convert("RGBA")
    return png_image


class DocxLoader(BaseLoader):
    """Adapter shim — delegates to ``DocxParser``; layers image captioning on top."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._parser = DocxParser()

    async def aload_document(self, file_path, metadata, save_markdown=False):
        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.DOCX,
            raw_bytes=raw_bytes,
            metadata=dict(metadata) if metadata else {},
        )
        processed = await self._parser.parse(core_doc)
        result = "\n\n".join(b.text for b in processed.text_blocks).strip()

        if not self.image_captioning:
            logger.info("Image captioning disabled. Ignoring images.")
            for block in processed.images:
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    result = result.replace(ref, "")
        else:
            pil_images: list[Image.Image] = []
            for block in processed.images:
                img = Image.open(BytesIO(block.image_bytes))
                img.load()
                pil_images.append(img)
            captions = await self.caption_images(pil_images, desc="Captioning embedded images")
            for block, caption in zip(processed.images, captions):
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    result = result.replace(ref, caption.replace("\\", "/"))

            result = await self.replace_markdown_images_with_captions(
                result,
                caption_data_uris=False,
                desc="Captioning linked images",
            )

        doc = Document(page_content=result, metadata=dict(metadata) if metadata else {})
        if save_markdown:
            self.save_content(result, str(file_path))
        return doc

    # ----- legacy helpers retained for test_docx_loader.py compatibility -----

    def get_images_from_zip(self, input_file):
        try:
            docx = zipfile.ZipFile(input_file, "r")
        except zipfile.BadZipFile:
            logger.warning("File is not a valid zip archive; skipping image extraction.", path=str(input_file))
            return []
        with docx:
            file_names = docx.namelist()
            image_files = [f for f in file_names if f.startswith("word/media/")]
            if not image_files:
                return []

            images_not_in_order, order = [], []
            for image_file in image_files:
                image_data = docx.read(image_file)
                image_extension = image_file.split(".")[-1].lower()
                try:
                    image = Image.open(BytesIO(image_data))
                    image = convert_to_png_image(image)
                    order_num = int(image_file.split("media/image")[1].split(f".{image_extension}")[0])
                except Exception as e:
                    logger.warning(f"Skipping unsupported media file {image_file}: {e}")
                    continue

                images_not_in_order.append(image)
                order.append(order_num)

            if not images_not_in_order:
                return []

            max_order = max(order)
            images = [None] * max_order
            for i, pos in enumerate(order):
                images[pos - 1] = images_not_in_order[i]
            return images
