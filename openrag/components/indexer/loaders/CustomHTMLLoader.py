from pathlib import Path

from langchain_community.document_loaders import UnstructuredHTMLLoader
from langchain_core.documents.base import Document

from .base import BaseLoader


class CustomHTMLLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def aload_document(self, file_path, metadata: dict = None):
        path = Path(file_path)
        loader = UnstructuredHTMLLoader(file_path=str(path), autodetect_encoding=True)
        doc = await loader.aload()

        s = ""
        for page_num, segment in enumerate(doc, start=1):
            s += segment.page_content.strip() + f"\n[PAGE_{page_num}]\n"

        return Document(page_content=s, metadata=metadata)
