"""Legacy BIFF8 coverage using an xlwt-generated workbook in tests/resources."""

from pathlib import Path
from struct import pack, pack_into
from types import SimpleNamespace

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
    assert "李明 | 0 | false | 5000000" in first.text
    assert "Zoë | 42 | false | -5000000.5" in first.text
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


def test_xls_short_stream_accepts_a_chain_ending_at_its_declared_length():
    compound = object.__new__(xls_parser._BoundedCompoundDocument)
    assert compound._get_stream(b"abcd", 0, [1, xls_parser.EOCSID], 2, 0, 3) == b"abc"


@pytest.mark.parametrize(("allocation", "size"), [([1, 2], 3), ([1, xls_parser.EOCSID], 2)])
def test_xls_short_stream_rejects_an_invalid_chain_before_reading(allocation, size, monkeypatch):
    def unexpected_read(*args):
        pytest.fail("invalid sector chain was passed to xlrd")

    monkeypatch.setattr(xls_parser.CompDoc, "_get_stream", unexpected_read)
    compound = object.__new__(xls_parser._BoundedCompoundDocument)
    with pytest.raises(xls_parser.CompDocError, match="sector|termination"):
        compound._get_stream(b"abcd", 0, allocation, 2, 0, size)


def test_xls_checks_directory_references_before_xlrd_builds_the_tree():
    directory = bytearray(3 * 128)
    pack_into("<i", directory, 76, 1)
    pack_into("<iii", directory, 128 + 68, -1, 2, -1)
    pack_into("<iii", directory, 256 + 68, -1, -1, -1)
    xls_parser._validate_directory(directory)
    pack_into("<i", directory, 128 + 68, 2)
    with pytest.raises(xls_parser.CompDocError, match="directory entry"):
        xls_parser._validate_directory(directory)


@pytest.mark.parametrize(
    ("opcode", "version", "stream_type"), [(0x0409, 0x0400, 5), (0x0809, 0x0400, 5), (0x0809, 0x0600, 0x10)]
)
def test_xls_rejects_unsupported_workbook_bof(opcode, version, stream_type):
    xls_parser._validate_workbook_bof(pack("<HHHH", 0x0809, 4, 0x0600, 5))
    with pytest.raises(xls_parser.CompDocError, match="BIFF5"):
        xls_parser._validate_workbook_bof(pack("<HHHH", opcode, 4, version, stream_type))


def _sheet_records(*records):
    header = pack("<HHHH", 0x0809, 4, 0x0600, 0x10)
    body = b"".join(pack("<HH", opcode, len(payload)) + payload for opcode, payload in records)
    return SimpleNamespace(mem=header + body + pack("<HH", 0x000A, 0), _sh_abs_posn=[0])


def test_xls_counts_all_hyperlink_ranges_toward_the_preflight_limit():
    first = (xls_parser.biffh.XL_HLINK, pack("<HHHH", 0, 1, 0, 1))
    second = (xls_parser.biffh.XL_HLINK, pack("<HHHH", 2, 3, 0, 1))
    xls_parser._check_sheet_area(_sheet_records(first, second), 0, 8)
    with pytest.raises(PipelineError, match="hyperlink cell limit"):
        xls_parser._check_sheet_area(_sheet_records(first, second), 0, 7)


@pytest.mark.parametrize("repeated", [False, True])
def test_xls_rejects_dimensions_after_cell_data_or_repeated_dimensions(repeated):
    dimensions = (xls_parser.biffh.XL_DIMENSION, bytes(14))
    cell = (xls_parser.biffh.XL_BLANK, pack("<HHH", 0, 0, 0))
    xls_parser._check_sheet_area(_sheet_records(dimensions, cell), 0, 1)
    first = dimensions if repeated else cell
    with pytest.raises(xls_parser.xlrd.XLRDError, match="dimensions"):
        xls_parser._check_sheet_area(_sheet_records(first, dimensions), 0, 1)
