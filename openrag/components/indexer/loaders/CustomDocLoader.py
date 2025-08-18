from pathlib import Path

from langchain_community.document_loaders import (
    UnstructuredODTLoader,
    UnstructuredWordDocumentLoader,
)
from langchain_core.documents.base import Document

from .base import BaseLoader


class CustomDocLoader(BaseLoader):
    doc_loaders = {
        ".docx": UnstructuredWordDocumentLoader,
        ".doc": UnstructuredWordDocumentLoader,
        ".odt": UnstructuredODTLoader,
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def aload_document(self, file_path, metadata: dict = None):
        path = Path(file_path)
        cls_loader = CustomDocLoader.doc_loaders.get(path.suffix, None)

        if cls_loader is None:
            raise ValueError(
                f"This loader only supports {CustomDocLoader.doc_loaders.keys()} format"
            )

        loader = cls_loader(
            file_path=str(file_path),
            mode="single",
        )
        pages = await loader.aload()

        s = ""
        for page_num, p in enumerate(pages, start=1):
            s = p.page_content.strip() + f"\n[PAGE_{page_num}]\n"

        return Document(page_content=s, metadata=metadata)
