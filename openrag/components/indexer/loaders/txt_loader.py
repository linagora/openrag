"""
Text and Markdown file loader implementation.
"""

from pathlib import Path

from components.indexer.loaders.base import BaseLoader
from langchain_community.document_loaders import TextLoader as LangchainTextLoader
from langchain_core.documents.base import Document
from utils.logger import get_logger

logger = get_logger()


class TextLoader(BaseLoader):
    """
    Loader for plain text files (.txt).
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        loader = LangchainTextLoader(file_path=str(path), autodetect_encoding=True)

        # Load document segments asynchronously
        doc_segments = await loader.aload()

        # Create final document
        content = doc_segments[0].page_content.strip()

        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(content, str(path))

        return doc


class MarkdownLoader(BaseLoader):
    """
    Loader for markdown files (.md).
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        loader = LangchainTextLoader(file_path=str(path), autodetect_encoding=True)

        # Load document segments asynchronously
        doc_segments = await loader.aload()

        # Create final document
        content = doc_segments[0].page_content.strip()

        # Caption any images in the markdown
        content = await self.replace_markdown_images_with_captions(content)

        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(text_content=content, path=str(path))
        return doc
