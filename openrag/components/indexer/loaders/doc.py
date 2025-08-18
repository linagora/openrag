import os
import tempfile
from spire.doc import Document, FileFormat
from .base import BaseLoader
from .markItdown import MarkItDownLoader


class DocLoader(BaseLoader):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.MDLoader = MarkItDownLoader(**kwargs)

    async def aload_document(self, file_path, metadata, save_markdown=False):
        """Here we convert the document to docx format, save it in local and then use the MarkItDownLoader
        to convert it to markdown."""
        document = Document()
        document.LoadFromFile(str(file_path))
        # file_path = "converted/sample2.docx"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
            file_path = temp_file.name
            document.SaveToFile(file_path, FileFormat.Docx2016)
        result_string = await self.MDLoader.aload_document(
            file_path, metadata, save_markdown
        )
        os.remove(file_path)
        document.Close()
        return result_string

    async def parse(self, file_path):
        document = Document()
        document.LoadFromFile(str(file_path))
        # file_path = "converted/sample.docx"
        document.SaveToFile(file_path, FileFormat.Docx2016)
        result_string = await self.MDLoader.parse(file_path)
        os.remove(file_path)
        document.Close()
        return result_string
