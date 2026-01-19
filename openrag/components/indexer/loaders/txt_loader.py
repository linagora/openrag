"""
Text and Markdown file loader implementation.
"""

import itertools
import re
from pathlib import Path

from components.indexer.loaders.base import BaseLoader
from langchain_community.document_loaders import TextLoader as LangchainTextLoader
from langchain_core.documents.base import Document
from tqdm.asyncio import tqdm
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
    Loader for plain text files (.txt).
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Pattern for HTTP/HTTPS images
        self._http_img_pattern = re.compile(r"!\[(.*?)\]\((https?://.*?)\)")
        # Pattern for data URI images (base64)
        self._data_uri_pattern = re.compile(r"!\[(.*?)\]\((data:image/[^;]+;base64,[^)]+)\)")

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

        # Find all types of images
        http_matches = self._http_img_pattern.findall(content)
        data_uri_matches = self._data_uri_pattern.findall(content)
        total_images = len(http_matches) + len(data_uri_matches)

        logger.debug(
            "Found images in markdown",
            http_images=len(http_matches),
            data_uri_images=len(data_uri_matches),
            total_images=total_images,
        )

        if total_images > 0 and self.image_captioning:
            # Check if URL captioning is enabled
            image_captioning_url = self.config.loader.get("image_captioning_url", False)

            # Process all images concurrently
            tasks = {}

            for alt, url in itertools.chain(http_matches, data_uri_matches):
                # Skip HTTP URLs if URL captioning is disabled
                if not image_captioning_url and url.startswith(("http://", "https://")):
                    continue
                markdown_syntax = f"![{alt}]({url})"
                tasks[markdown_syntax] = self.get_image_description(url)

            if tasks:  # Only process if there are tasks
                image_to_description = await tqdm.gather(*tasks.values(), desc="Captioning images")
                image_to_description = dict(zip(tasks.keys(), image_to_description))

                # Replace images with descriptions
                logger.debug("Replacing image references", image_count=len(image_to_description))
                for md_syntax, description in image_to_description.items():
                    content = content.replace(md_syntax, description)

        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(text_content=content, path=str(path))
        return doc
