from __future__ import annotations

import pytest
from core.config.table_reconstruction import TableReconstructionConfig
from core.indexing.structure_normalizer import DocumentStructureNormalizer
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.stages.normalize_structure import normalize_structure_stage


class FakeNormalizer(DocumentStructureNormalizer):
    def __init__(self, output: ProcessedDocument | None = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = []

    async def normalize(self, document, processed_document, config):
        self.calls.append((document, processed_document, config))
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output


@pytest.mark.asyncio
async def test_normalize_structure_stage_replaces_the_processed_document():
    document = Document(filename="sample.pdf", content_type=DocumentType.PDF)
    parsed = ProcessedDocument(text_blocks=[TextBlock(text="raw")])
    normalized = ProcessedDocument(text_blocks=[TextBlock(text="raw")], normalized_text_blocks=[TextBlock(text="row")])
    normalizer = FakeNormalizer(output=normalized)
    row = {"document": document, "processed_document": parsed}
    config = TableReconstructionConfig(mode="automatic")

    result = await normalize_structure_stage(row, normalizer, config)

    assert result["processed_document"] is normalized
    assert result["stage"] == "structure_normalized"
    assert normalizer.calls == [(document, parsed, config)]


@pytest.mark.asyncio
async def test_normalize_structure_stage_fails_open_on_an_unexpected_error():
    document = Document(filename="sample.pdf", content_type=DocumentType.PDF)
    parsed = ProcessedDocument(text_blocks=[TextBlock(text="usable parser output")])
    row = {"document": document, "processed_document": parsed, "token": "secret"}

    result = await normalize_structure_stage(
        row,
        FakeNormalizer(error=RuntimeError("layout failed")),
        TableReconstructionConfig(mode="automatic"),
    )

    assert result["processed_document"].effective_text_blocks()[0].text == "usable parser output"
    assert result["processed_document"].normalization_report.status == "partial_fallback"
    assert result["stage"] == "structure_normalization_fallback"
    assert "error" not in result
    assert "token" not in result
