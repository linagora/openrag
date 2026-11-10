"""End-to-end XLSX ingestion: upload through the API, read the stored chunks back.

Covers the path unit tests cannot: the upload route, the indexing worker, the
parser's single worksheet pass (two side-by-side tables, so one waits in the
spool), the embedder and the vector store. The workbook is written as raw
OOXML so the test needs no spreadsheet library on the runner.
"""

import io
import json
import re
import zipfile
from urllib.parse import urlparse

from conftest import wait_for_task

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _column(index: int) -> str:
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[index - 1]


def _cell(row: int, column: int, value) -> str:
    ref = f"{_column(column)}{row}"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def _sheet(rows: dict[int, dict[int, object]], *, hidden: set[int] = frozenset(), sheet_pr="", tail="") -> str:
    body = []
    for row in sorted(rows):
        attributes = ' hidden="1"' if row in hidden else ""
        cells = "".join(_cell(row, column, value) for column, value in sorted(rows[row].items()))
        body.append(f'<row r="{row}"{attributes}>{cells}</row>')
    return f'<?xml version="1.0"?><worksheet xmlns="{_MAIN}">{sheet_pr}<sheetData>{"".join(body)}</sheetData>{tail}</worksheet>'


def _workbook() -> bytes:
    # Sheet 1: two tables side by side (columns A-B and E-F, two blank columns
    # between them), so the second one is served from the spool.
    blocks = {1: {1: "Item", 2: "Qty", 5: "City", 6: "Population"}}
    for row in range(2, 42):
        blocks[row] = {1: f"item-{row}", 2: row, 5: f"city-{row}", 6: row * 1000}
    # Sheet 2: saved with an active filter (Status = open) hiding the closed
    # tickets, plus one row hidden by hand outside the filter range.
    tickets = {1: {1: "Ticket", 2: "Status"}}
    hidden = set()
    for number in range(1, 21):
        row = number + 1
        status = "open" if number % 4 == 0 else "closed"
        tickets[row] = {1: f"T-{number}", 2: status}
        if status == "closed":
            hidden.add(row)
    tickets[23] = {1: "T-99", 2: "draft"}
    hidden.add(23)

    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
            '<sheet name="Blocks" sheetId="1" r:id="rId1"/><sheet name="Tickets" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{_REL}/worksheet" Target="worksheets/sheet2.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": _sheet(blocks),
        "xl/worksheets/sheet2.xml": _sheet(
            tickets,
            hidden=hidden,
            sheet_pr='<sheetPr filterMode="1"/>',
            tail='<autoFilter ref="A1:B21"/>',
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def _stored_chunks(api_client, partition: str, file_id: str) -> list[dict]:
    response = api_client.get(f"/partition/{partition}/file/{file_id}")
    assert response.status_code == 200, response.text
    chunks = []
    for document in response.json()["documents"]:
        extract = api_client.get(urlparse(document["link"]).path)
        assert extract.status_code == 200, extract.text
        chunks.append(extract.json())
    return chunks


class TestXlsxIngestion:
    def test_workbook_with_side_by_side_tables_and_a_filter_is_fully_indexed(self, api_client, created_partition):
        file_id = "xlsx-e2e-001"
        response = api_client.post(
            f"/indexer/partition/{created_partition}/file/{file_id}",
            files={"file": ("e2e.xlsx", _workbook(), XLSX_MIME)},
            data={"metadata": json.dumps({})},
        )
        assert response.status_code in (200, 201, 202), response.text
        wait_for_task(api_client, response.json()["task_status_url"].split("/")[-1])

        chunks = _stored_chunks(api_client, created_partition, file_id)
        text = "\n".join(chunk["page_content"] for chunk in chunks)
        metadata = [chunk["metadata"] for chunk in chunks]

        # Both side-by-side tables, first and last rows, each under its own header.
        for value in ("item-2", "item-41", "city-2", "city-41"):
            assert value in text, f"{value} missing from the indexed chunks"
        table_ids = {(m.get("sheet_name"), m.get("table_id")) for m in metadata if m.get("sheet_name") == "Blocks"}
        assert len(table_ids) == 2, table_ids

        # Rows hidden by the active filter are indexed; the row hidden by hand is not.
        for number in range(1, 21):
            assert re.search(rf"\| T-{number} \|", text), f"T-{number} missing from the indexed chunks"
        assert "T-99" not in text
        tickets = [m for m in metadata if m.get("sheet_name") == "Tickets"]
        assert tickets and all(m.get("filtered_rows_included") == 15 for m in tickets)
        assert all(m.get("hidden_rows_skipped") == 1 for m in tickets)
