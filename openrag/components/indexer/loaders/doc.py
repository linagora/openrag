"""
Legacy ``.doc`` file loader implementation.

``DocLoader`` is now a thin :class:`BaseLoader` adapter that delegates
to :class:`core.indexing.parsers.doc_parser.DocParser` (which itself
runs Spire.Doc → .docx conversion and then ``DocxParser``) and layers
VLM captioning of embedded images on top. New code should call the
core parser directly; this shim keeps the legacy loader-discovery path
alive until consumers migrate.
"""

import asyncio
import os
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.doc_parser import DocParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document as LCDocument
from PIL import Image
from utils.logger import get_logger

from .base import BaseLoader
from .docx import DocxLoader

os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"

logger = get_logger()


class DocLoader(BaseLoader):
    """Adapter shim — delegates to ``DocParser``; layers image captioning on top."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.MDLoader = DocxLoader(**kwargs)

        self._parser = DocParser()

    async def aload_document(self, file_path, metadata, save_markdown=False):
        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.DOC,
            raw_bytes=raw_bytes,
            metadata=dict(metadata) if metadata else {},
        )
        processed = await self._parser.parse(core_doc)
        result = "\n\n".join(b.text for b in processed.text_blocks).strip()

        if not self.image_captioning:
            logger.info("Image captioning disabled. Ignoring images.")
        else:
            if processed.images:
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

        doc = LCDocument(page_content=result, metadata=dict(metadata) if metadata else {})
        if save_markdown:
            self.save_content(result, str(file_path))
        return doc
