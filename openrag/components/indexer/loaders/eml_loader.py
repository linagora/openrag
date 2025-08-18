from langchain_core.documents.base import Document
from .base import BaseLoader
import eml_parser


class EmlLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def aload_document(
        self, file_path, metadata: dict = None, save_markdown: bool = False
    ):
        try:
            with open(file_path, "rb") as fhdl:
                raw_email = fhdl.read()

            ep = eml_parser.EmlParser(include_raw_body=True)
            parsed_eml = ep.decode_email_bytes(raw_email)
            content_body = parsed_eml["body"][0]["content"]
        except Exception as e:
            raise ValueError(f"Failed to parse the EML file {file_path}: {e}")

        document = Document(page_content=content_body, metadata=metadata)
        return document
