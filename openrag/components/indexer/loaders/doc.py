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
        document = Document()
        document.LoadFromFile(str(file_path))

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
                temp_path = temp_file.name
                document.SaveToFile(temp_path, FileFormat.Docx2016)
            result = await self.MDLoader.aload_document(temp_path, metadata, save_markdown)
            document.Close()
            return result
        except Exception as e:
            logger.warning(
                f"Spire.Doc conversion to .docx failed, falling back to text extraction: {e}"
            )
            text = document.GetText()
            document.Close()
            doc = LCDocument(page_content=text, metadata=metadata)
            if save_markdown:
                self.save_content(text, str(file_path))
            return doc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
