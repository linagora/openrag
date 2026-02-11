import asyncio
import os
import tempfile

from spire.doc import Document, FileFormat

from .base import BaseLoader
from .docx import DocxLoader

os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"  # Disable Globalization


class DocLoader(BaseLoader):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.MDLoader = DocxLoader(**kwargs)

    def _convert_doc_to_docx(self, file_path):
        """Convert .doc to .docx using Spire.Doc (blocking operations in thread pool)."""
        document = Document()
        document.LoadFromFile(str(file_path))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
            temp_path = temp_file.name
            document.SaveToFile(temp_path, FileFormat.Docx2016)
        return document, temp_path

    async def aload_document(self, file_path, metadata, save_markdown=False):
        """Here we convert the document to docx format, save it in local and then use the MarkItDownLoader
        to convert it to markdown."""
        document, temp_path = await asyncio.to_thread(self._convert_doc_to_docx, file_path)
        try:
            result_string = await self.MDLoader.aload_document(temp_path, metadata, save_markdown)
        finally:
            await asyncio.to_thread(os.remove, temp_path)
            document.Close()
        return result_string
