"""Legacy BIFF8 coverage using an xlwt-generated workbook in tests/resources."""

from pathlib import Path

import pytest
from core.indexing.parsers.tabular import xls_parser
from core.indexing.parsers.tabular.xls_parser import XlsParser
from core.models.document import Document, DocumentType
from core.utils.exceptions import PipelineError
from services.workers.parsers.parser_dispatcher import ParserDispatcher

WORKBOOK = Path(__file__).resolve().parents[4] / "resources" / "legacy_workbook.xls"


@pytest.mark.parametrize("source", ["source_path", "raw_bytes"])
async def test_xls_dispatcher_extracts_visible_sheets_with_physical_coordinates(source):
    value = str(WORKBOOK) if source == "source_path" else WORKBOOK.read_bytes()
    document = Document(filename="Workbook.XLS", content_type=DocumentType.XLS, **{source: value})
    result = await ParserDispatcher(None).parse(document)
    first, second = result.text_blocks
    assert first.metadata["sheet_name"] == "Quarter's sales"
    assert first.metadata["table_row_numbers"] == [4, 6, 8]
    assert first.metadata["table_worksheet_column_indices"] == [3, 4, 5, 6, 7]
    assert first.metadata["table_columns"] == ["ID", "ID_2", "Amount", "Paid", "Due"]
    assert first.metadata["cell_range"] == "'Quarter''s sales'!C4:G8"
    assert "René &#124; Paris | 17.25 | true | 2026-11-24" in first.text
    assert "李明 | 0 | false" in first.text
    assert second.metadata["sheet_name"] == "Single row"
    assert second.metadata["table_header_row"] is None
    assert "Unlabelled notes" in second.text
    assert all("SECRET" not in b.text for b in result.text_blocks)


def test_xls_batches_preserve_table_identity_and_numbering():
    document = Document(filename="book.xls", raw_bytes=WORKBOOK.read_bytes())
    batches = list(XlsParser(batch_size=2).iter_batches(document))
    assert [b.metadata["table_row_numbers"] for b in batches] == [[4, 6], [8], [1]]
    assert [b.metadata["table_batch_index"] for b in batches] == [1, 2, 1]
    assert batches[0].metadata["table_id"] == batches[1].metadata["table_id"] != batches[2].metadata["table_id"]


@pytest.mark.parametrize("data", [b"text renamed .xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"])
def test_invalid_xls_reports_a_parser_error(data):
    with pytest.raises(PipelineError) as error:
        list(XlsParser().iter_batches(Document(filename="bad.xls", raw_bytes=data)))
    assert error.value.code == "INVALID_XLS"


def test_xls_input_limit_applies_before_reading_workbook(monkeypatch):
    monkeypatch.setattr(xls_parser, "_MAX_FILE_BYTES", 64)
    document = Document(filename="book.xls", source_path=str(WORKBOOK))
    with pytest.raises(PipelineError) as error:
        list(XlsParser().iter_batches(document))
    assert error.value.code == "XLS_LIMIT_EXCEEDED"


def test_xls_rejects_oversized_sheet_before_xlrd_materializes_it(monkeypatch):
    def unexpected_sheet_load(*args):
        pytest.fail("oversized sheet was loaded")

    monkeypatch.setattr(xls_parser, "_MAX_SHEET_AREA", 20)
    monkeypatch.setattr(xls_parser.xlrd.book.Book, "sheet_by_index", unexpected_sheet_load)
    document = Document(filename="book.xls", source_path=str(WORKBOOK))
    with pytest.raises(PipelineError) as error:
        list(XlsParser().iter_batches(document))
    assert error.value.code == "XLS_LIMIT_EXCEEDED"
