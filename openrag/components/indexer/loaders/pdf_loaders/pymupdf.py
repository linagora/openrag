import asyncio
from pathlib import Path
from langchain_community.document_loaders import PyMuPDFLoader as pymupdf_loader
from langchain_core.documents.base import Document
import pymupdf4llm

from ..base import BaseLoader


class PyMuPDFLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def aload_document(
        self, file_path, metadata: dict = None, save_markdown=False
    ):
        loader = pymupdf_loader(
            file_path=Path(file_path),
        )
        pages = await loader.aload()

        s = ""
        for page_num, segment in enumerate(pages, start=1):
            s = segment.page_content.strip() + f"\n[PAGE_{page_num}]\n"

        doc = Document(page_content=s, metadata=metadata)
        if save_markdown:
            self.save_content(s, str(file_path))
        return doc


class PyMuPDF4LLMLoader(BaseLoader):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    async def aload_document(
        self, file_path, metadata: dict = None, save_markdown=False
    ):
        pages = await asyncio.to_thread(
            pymupdf4llm.to_markdown,
            file_path,
            write_images=False,
            page_chunks=True,
        )

        s = ""
        for page_num, segment in enumerate(pages, start=1):
            s = segment.page_content.strip() + f"\n[PAGE_{page_num}]\n"

        doc = Document(page_content=s, metadata=metadata)
        if save_markdown:
            self.save_content(s, str(file_path))
        return doc
