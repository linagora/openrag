"""
Image file loader implementation.

``ImageLoader`` is now a thin :class:`BaseLoader` adapter that delegates
decode (raster + SVG) to
:class:`core.indexing.parsers.image_parser.ImageParser` and then layers
VLM captioning on top via the ``BaseLoader`` mixin. The legacy
``ImageLoadError`` contract is preserved on decode failure. New code
should call the core parser directly; this shim keeps the legacy
loader-discovery path alive until consumers migrate.
"""

import asyncio
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.image_parser import ImageParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents import Document
from PIL import Image
from utils.logger import get_logger

from .base import BaseLoader

log = get_logger()


class ImageLoadError(Exception):
    """Raised when an image file cannot be loaded or converted."""


class ImageLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # ``min_pixels=0`` so the parser does not drop small images; the
        # size threshold is enforced by ``get_image_description`` (which
        # returns the legacy "Image too small for captioning" marker).
        self._parser = ImageParser(min_pixels=0)

    async def aload_document(self, file_path, metadata=None, save_markdown=False):
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        try:
            raw_bytes = await asyncio.to_thread(path.read_bytes)
        except Exception as e:
            log.error(
                "Failed to read image file",
                file_path=str(path),
                error_type=type(e).__name__,
                error=str(e),
            )
            raise ImageLoadError(f"Cannot load image '{path.name}': {type(e).__name__}") from e

        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.IMAGE,
            raw_bytes=raw_bytes,
            metadata=metadata,
        )
        processed = await self._parser.parse(core_doc)
        if not processed.images:
            raise ImageLoadError(f"Cannot load image '{path.name}': failed to decode")

        img = Image.open(BytesIO(processed.images[0].image_bytes))
        img.load()
        description = await self.get_image_description(image_data=img)

        doc = Document(page_content=description, metadata=metadata)
        if save_markdown:
            self.save_content(description, str(path))
        return doc
