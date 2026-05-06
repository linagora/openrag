"""
Text and Markdown file loader implementation.

``TextLoader`` and ``MarkdownLoader`` are now thin :class:`BaseLoader`
adapters that delegate extraction to the corresponding core parsers
(:class:`core.indexing.parsers.text_parser.TextParser`,
:class:`core.indexing.parsers.markdown_parser.MarkdownParser`). Image
captioning is layered on top of the markdown adapter via the
``BaseLoader`` mixin. New code should call the core parsers directly;
these shims keep the legacy loader-discovery path alive until consumers
migrate.
"""

import asyncio
from pathlib import Path

from components.indexer.loaders.base import BaseLoader
from core.indexing.parsers.markdown_parser import MarkdownParser
from core.indexing.parsers.text_parser import TextParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from utils.logger import get_logger

logger = get_logger()


class TextLoader(BaseLoader):
    """Adapter shim — delegates to ``TextParser`` and returns a LangChain ``Document``."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._parser = TextParser()

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.TEXT,
            raw_bytes=raw_bytes,
            metadata=metadata,
        )
        processed = await self._parser.parse(core_doc)
        content = "\n\n".join(block.text for block in processed.text_blocks).strip()

        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(content, str(path))

        return doc


class MarkdownLoader(BaseLoader):
    """Adapter shim — delegates to ``MarkdownParser`` and layers image captioning on top."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._parser = MarkdownParser()

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.MARKDOWN,
            raw_bytes=raw_bytes,
            metadata=metadata,
        )
        processed = await self._parser.parse(core_doc)
        content = "\n\n".join(block.text for block in processed.text_blocks).strip()

        content = await self.replace_markdown_images_with_captions(content)

        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(text_content=content, path=str(path))
        return doc
