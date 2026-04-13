import os
import tempfile

from langchain_core.documents.base import Document as LCDocument
from spire.doc import Document, FileFormat
from utils.logger import get_logger

from .base import BaseLoader
from .docx import DocxLoader

os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"  # Disable Globalization

logger = get_logger()


class DocLoader(BaseLoader):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.MDLoader = DocxLoader(**kwargs)

    async def aload_document(self, file_path, metadata, save_markdown=False):
        """Convert .doc to .docx format, then use DocxLoader to convert to markdown.
        Falls back to plain text extraction if the .docx conversion fails."""
        temp_path = None
        document = Document()
        try:
            document.LoadFromFile(str(file_path))
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
                temp_path = temp_file.name
                document.SaveToFile(temp_path, FileFormat.Docx2016)
        except Exception as e:
            logger.bind(file_id=metadata.get("file_id"), partition=metadata.get("partition")).warning(
                f"Spire.Doc conversion to .docx failed, falling back to text extraction: {e}"
            )
            text = document.GetText()
            doc = LCDocument(page_content=text, metadata=metadata)
            if save_markdown:
                self.save_content(text, str(file_path))
            return doc
        else:
            result = await self.MDLoader.aload_document(temp_path, metadata, save_markdown)
            return result
        finally:
            document.Close()
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
