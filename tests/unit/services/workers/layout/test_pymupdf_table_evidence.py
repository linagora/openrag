from pathlib import Path

import pytest
from core.models.document import Document, DocumentType
from services.workers.layout import PyMuPDFTableEvidenceProvider

FIXTURE = Path(__file__).parents[4] / "resources" / "cross_page_table_rows_803_805.pdf"


@pytest.mark.asyncio
async def test_adapter_exposes_tables_and_sparse_continuation_evidence():
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )

    pages = await PyMuPDFTableEvidenceProvider().collect(document, {1, 2, 3})

    assert [len(page.tables) for page in pages] == [1, 0, 1]
    assert len(pages[0].tables[0].column_bounds) == 5
    page_two_body = [word for word in pages[1].words if word.bbox[3] < 0.90]
    assert page_two_body
    assert min(word.bbox[0] for word in page_two_body) > pages[0].tables[0].column_bounds[-1][0]
