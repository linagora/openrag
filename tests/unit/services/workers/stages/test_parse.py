import pytest
from core.indexing.parsers.document_parser import DocumentParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.stages.parse import parse_stage


class FakeParser(DocumentParser):
    def __init__(self, output: ProcessedDocument | None = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.seen_documents: list[Document] = []

    async def parse(self, document: Document) -> ProcessedDocument:
        self.seen_documents.append(document)
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output

    def supported_types(self) -> list[str]:
        return [DocumentType.TEXT.value]


@pytest.mark.asyncio
async def test_parse_stage_mutates_row_with_processed_document_and_scrubs_credentials():
    document = Document(id="doc-1", filename="note.txt", content_type=DocumentType.TEXT, text="hello")
    processed = ProcessedDocument(
        document_id="doc-1",
        text_blocks=[TextBlock(text="hello", page_number=1)],
        metadata={"file_id": "file-1"},
        page_count=1,
    )
    parser = FakeParser(output=processed)
    row = {
        "document": document,
        "credentials": {"api_key": "secret"},
        "token": "secret-token",
    }

    result = await parse_stage(row, parser)

    assert result is row
    assert parser.seen_documents == [document]
    assert row["processed_document"] == processed
    assert row["stage"] == "parsed"
    assert "error" not in row
    assert "credentials" not in row
    assert "token" not in row


@pytest.mark.asyncio
async def test_parse_stage_marks_error_and_scrubs_credentials_when_parser_fails():
    document = Document(id="doc-1", filename="note.txt", content_type=DocumentType.TEXT, text="hello")
    row = {"document": document, "api_key": "secret"}

    with pytest.raises(ValueError, match="parse failed"):
        await parse_stage(row, FakeParser(error=ValueError("parse failed")))

    assert row["stage"] == "parse_failed"
    assert row["error"] == "parse failed"
    assert "api_key" not in row


@pytest.mark.asyncio
async def test_parse_stage_requires_document_in_row():
    row = {"api_key": "secret"}

    with pytest.raises(ValueError, match="document"):
        await parse_stage(row, FakeParser())

    assert row["stage"] == "parse_failed"
    assert row["error"] == "parse_stage row must contain a Document under 'document'"
    assert "api_key" not in row
