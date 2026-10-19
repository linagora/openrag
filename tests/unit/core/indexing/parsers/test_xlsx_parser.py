from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from core.indexing.parsers.tabular import xlsx_parser as module
from core.indexing.parsers.tabular.xlsx_parser import XlsxLimits, XlsxParser
from core.models.document import Document, DocumentType
from core.utils.exceptions import NoIndexableContentError, PipelineError
from openpyxl import Workbook


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    people = workbook.active
    people.title = "People"
    people.append([])
    people.append(["name", "", "active", "joined"])
    people.append(["Alice", "Paris", True, date(2025, 1, 2)])
    people.append([])
    people.append(["Bob", "Lyon", False, date(2025, 2, 3)])

    projects = workbook.create_sheet("Projects")
    projects.append(["name", "summary", ""])
    projects.append(["Access", "Public-service accessibility", "owner@example.org"])

    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    hidden.append(["secret"])
    hidden.append(["must not be indexed"])

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.mark.parametrize("backend", ["calamine", "openpyxl"])
def test_extracts_visible_sheets_with_physical_rows_and_stable_metadata(backend: str):
    parser = XlsxParser(batch_size=1, backend=backend, fallback_backend=None)
    document = Document(filename="people.xlsx", content_type=DocumentType.XLSX, raw_bytes=_workbook_bytes())

    blocks = list(parser.iter_batches(document))

    assert [block.metadata["sheet_name"] for block in blocks] == ["People", "People", "Projects"]
    assert [block.metadata["table_row_numbers"] for block in blocks] == [[3], [5], [2]]
    assert blocks[0].metadata["table_columns"] == ["name", "column_2", "active", "joined"]
    assert blocks[0].metadata["table_id"] == "sheet:0"
    assert blocks[2].metadata["table_id"] == "sheet:1"
    assert blocks[2].metadata["table_columns"] == ["name", "summary", "column_3"]
    assert blocks[0].metadata["cell_range"] == "'People'!A2:D3"
    assert "2025-01-02" in blocks[0].text
    assert "true" in blocks[0].text
    assert "must not be indexed" not in "\n".join(block.text for block in blocks)


def test_falls_back_before_emitting_any_batch(monkeypatch):
    def fail(_source):
        raise RuntimeError("backend unavailable")
        yield

    monkeypatch.setattr(module, "_calamine_sheets", fail)
    parser = XlsxParser(backend="calamine", fallback_backend="openpyxl")

    blocks = list(
        parser.iter_batches(
            Document(filename="people.xlsx", content_type=DocumentType.XLSX, raw_bytes=_workbook_bytes())
        )
    )

    assert blocks
    assert {block.metadata["spreadsheet_backend"] for block in blocks} == {"openpyxl"}
    assert all(block.metadata["spreadsheet_fallback_used"] for block in blocks)


def test_rejects_non_xlsx_archives_and_resource_amplification():
    document = Document(filename="fake.xlsx", content_type=DocumentType.XLSX, raw_bytes=b"not a zip")
    with pytest.raises(PipelineError, match="valid XLSX"):
        list(XlsxParser(backend="openpyxl", fallback_backend=None).iter_batches(document))

    document = Document(filename="people.xlsx", content_type=DocumentType.XLSX, raw_bytes=_workbook_bytes())
    parser = XlsxParser(
        backend="openpyxl",
        fallback_backend=None,
        limits=XlsxLimits(max_uncompressed_bytes=1),
    )
    with pytest.raises(PipelineError, match="extraction limit"):
        list(parser.iter_batches(document))


def test_rejects_workbook_without_visible_data_rows():
    workbook = Workbook()
    workbook.active.append(["header"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    document = Document(filename="empty.xlsx", content_type=DocumentType.XLSX, raw_bytes=output.getvalue())
    with pytest.raises(NoIndexableContentError, match="no visible worksheet"):
        list(XlsxParser(backend="openpyxl", fallback_backend=None).iter_batches(document))


def test_names_a_trailing_header_cell_when_its_column_contains_data():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name"])
    sheet.append(["Alice", "unexpected"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    document = Document(filename="wide.xlsx", content_type=DocumentType.XLSX, raw_bytes=output.getvalue())
    [block] = list(XlsxParser(backend="openpyxl", fallback_backend=None).iter_batches(document))

    assert block.metadata["table_columns"] == ["name", "column_2"]
    assert "unexpected" in block.text
