"""Read legacy Excel worksheets without executing workbook content."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import time
from html import escape
from io import StringIO
from struct import error as StructError
from struct import unpack_from

import xlrd
from core.indexing.parsers.registry import parser_registry
from core.indexing.parsers.tabular.base import TabularBatchParser, markdown_row, normalize_unique_headers
from core.indexing.validators import ENCRYPTED_OFFICE_FILE_MESSAGE
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError, PipelineError
from xlrd import biffh
from xlrd.compdoc import CompDocError

_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_SHEETS = 128
_MAX_SHEET_AREA = 2_000_000
_MAX_WORKBOOK_AREA = 5_000_000
_MAX_BATCH_BYTES = 2 * 1024 * 1024
_MAX_RENDERED_BYTES = 32 * 1024 * 1024
_EMPTY_CELL_TYPES = {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}
# Cell records xlrd materializes for BIFF5–8, including formatting-only blanks.
_MULTIPLE_CELLS = {biffh.XL_MULRK, biffh.XL_MULBLANK}
_CELL_RECORDS = {
    biffh.XL_NUMBER,
    biffh.XL_LABELSST,
    biffh.XL_LABEL,
    biffh.XL_RSTRING,
    biffh.XL_RK,
    biffh.XL_BOOLERR,
    biffh.XL_BLANK,
    *biffh.XL_FORMULA_OPCODES,
    *_MULTIPLE_CELLS,
}


def _limit(reason: str) -> PipelineError:
    return PipelineError(reason, code="XLS_LIMIT_EXCEEDED", status_code=422)


def _read_workbook(document: Document) -> xlrd.book.Book:
    if document.source_path:
        with open(document.source_path, "rb") as stream:
            content = stream.read(_MAX_FILE_BYTES + 1)
    else:
        content = document.raw_bytes or b""
    if len(content) > _MAX_FILE_BYTES:
        raise _limit("XLS workbook exceeds the 16 MiB file limit")
    if not content.startswith(xlrd.XLS_SIGNATURE):
        raise PipelineError("File is not a valid binary XLS workbook", code="INVALID_XLS", status_code=422)
    return xlrd.open_workbook(
        file_contents=content, on_demand=True, formatting_info=True, ragged_rows=True, logfile=StringIO()
    )


def _check_sheet_area(book: xlrd.book.Book, index: int, limit: int) -> None:
    """Read BIFF cell coordinates before xlrd can allocate a sparse sheet's padded rows.

    xlrd exposes no size hook for an unloaded sheet. Use the workbook stream
    and offsets its on-demand reader already resolved. DIMENSIONS is only a
    hint and can be missing or stale, so check actual cell records instead.
    """
    data = book.mem
    position = book._sh_abs_posn[index]
    if unpack_from("<H", data, position)[0] not in biffh.bofcodes:
        raise xlrd.XLRDError("Missing BIFF worksheet beginning")
    rows = columns = 0
    in_embedded_object = False
    first_record = True
    while position < len(data):
        code, length = unpack_from("<HH", data, position)
        start = position + 4
        position = start + length
        if position > len(data):
            raise xlrd.XLRDError("Incomplete BIFF worksheet record")
        if first_record:
            first_record = False
        elif code == biffh.XL_EOF:
            if not in_embedded_object:
                return
            in_embedded_object = False
        elif not in_embedded_object:
            if code in biffh.bofcodes:
                in_embedded_object = True
            elif code in _CELL_RECORDS:
                if length < 4 or (code in _MULTIPLE_CELLS and length < 8):
                    raise xlrd.XLRDError("Incomplete BIFF cell record")
                row, column = unpack_from("<HH", data, start)
                if code in _MULTIPLE_CELLS:
                    column = unpack_from("<H", data, position - 2)[0]
                rows = max(rows, row + 1)
                columns = max(columns, column + 1)
                if rows * columns > limit:
                    raise _limit("XLS workbook exceeds the worksheet area limit")
    raise xlrd.XLRDError("Missing BIFF worksheet ending")


def _cell_text(value: str | float | int, kind: int, datemode: int) -> str:
    if kind == xlrd.XL_CELL_BOOLEAN:
        return "true" if value else "false"
    if kind == xlrd.XL_CELL_DATE:
        date = xlrd.xldate_as_datetime(value, datemode)
        if 0 <= value < 1:
            return date.time().isoformat()
        return date.date().isoformat() if date.time() == time() else date.isoformat()
    if kind == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(value, "#ERROR!")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _visible_rows(sheet: xlrd.sheet.Sheet) -> Iterator[tuple[int, list[int], list[str | float | int]]]:
    for row in range(sheet.nrows):
        info = sheet.rowinfo_map.get(row)
        hidden = info.hidden if info is not None else sheet.default_row_hidden
        if hidden:
            continue
        yield row, sheet.row_types(row), sheet.row_values(row)


def _worksheet_batches(sheet: xlrd.sheet.Sheet, index: int, datemode: int, batch_size: int) -> Iterator[TextBlock]:
    hidden_columns = {col for col, info in sheet.colinfo_map.items() if info.hidden}
    columns = set()
    first_row = last_row = None
    first_cells: dict[int, str] = {}
    first_all_text = True
    for row, kinds, values in _visible_rows(sheet):
        occupied = {
            col
            for col, kind in enumerate(kinds)
            if col not in hidden_columns
            and kind not in _EMPTY_CELL_TYPES
            and (kind != xlrd.XL_CELL_TEXT or values[col].strip())
        }
        if not occupied:
            continue
        columns.update(occupied)
        if first_row is None:
            first_row = row
            first_all_text = all(kinds[col] == xlrd.XL_CELL_TEXT for col in occupied)
            if first_all_text:
                first_cells = {col: values[col] for col in occupied}
        last_row = row
    if first_row is None:
        return

    cols = sorted(columns)
    header_row = first_row if first_row != last_row and first_all_text else None
    headers = normalize_unique_headers(
        [first_cells.get(col, "") for col in cols] if header_row is not None else [f"column_{col + 1}" for col in cols]
    )
    table_header = markdown_row(headers) + "\n" + markdown_row(["---"] * len(cols))
    context = f"\n\nWorksheet: {escape(' '.join(sheet.name.split()), quote=False)}"
    base_bytes = len((table_header + context).encode())
    physical_columns = [col + 1 for col in cols]
    start_col, end_col = xlrd.colname(cols[0]), xlrd.colname(cols[-1])
    sheet_ref = "'" + sheet.name.replace("'", "''") + "'!"
    batch_index = 0

    def block(lines: list[str], numbers: list[int]) -> TextBlock:
        return TextBlock(
            text="\n".join([table_header, *lines]) + context,
            block_type="table",
            metadata={
                "tabular_format": "xls",
                "spreadsheet_backend": "xlrd",
                "sheet_name": sheet.name,
                "sheet_index": index,
                "worksheet_context": context,
                "table_id": f"sheet:{index}:table:1",
                "table_index": 1,
                "worksheet_table_count": 1,
                "table_batch_index": batch_index,
                "table_columns": headers,
                "table_header_row": None if header_row is None else header_row + 1,
                "table_worksheet_column_indices": physical_columns,
                "table_row_start": numbers[0],
                "table_row_end": numbers[-1],
                "table_row_numbers": numbers,
                "table_range": f"{sheet_ref}{start_col}{first_row + 1}:{end_col}{last_row + 1}",
                "cell_range": f"{sheet_ref}{start_col}{numbers[0]}:{end_col}{numbers[-1]}",
                "csv_columns": headers,
                "csv_row_start": numbers[0],
                "csv_row_end": numbers[-1],
                "csv_row_numbers": numbers,
            },
        )

    lines, numbers, size = [], [], base_bytes
    for row, kinds, values in _visible_rows(sheet):
        if row == header_row:
            continue
        cells = [
            _cell_text(values[col], kinds[col], datemode)
            if col < len(kinds) and kinds[col] not in _EMPTY_CELL_TYPES
            else ""
            for col in cols
        ]
        if not any(value.strip() for value in cells):
            continue
        line = markdown_row(cells)
        line_bytes = 1 + len(line.encode())
        if base_bytes + line_bytes > _MAX_BATCH_BYTES:
            raise _limit(f"XLS row {row + 1} exceeds the 2 MiB batch limit")
        if lines and (len(lines) == batch_size or size + line_bytes > _MAX_BATCH_BYTES):
            batch_index += 1
            yield block(lines, numbers)
            lines, numbers, size = [], [], base_bytes
        lines.append(line)
        numbers.append(row + 1)
        size += line_bytes
    if lines:
        batch_index += 1
        yield block(lines, numbers)


@parser_registry.register("xls")
class XlsParser(TabularBatchParser):
    """Parse the CFB-based XLS format, one visible worksheet at a time."""

    tabular_format = "xls"

    def __init__(self, *, batch_size: int = 1_000):
        if type(batch_size) is not int or batch_size < 1:
            raise ConfigError("XLS batch_size must be a positive integer")
        self.batch_size = batch_size

    def supported_types(self) -> list[str]:
        return ["xls"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(
            lambda: ProcessedDocument(
                document_id=document.id, text_blocks=list(self.iter_batches(document)), metadata=dict(document.metadata)
            )
        )

    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        area = rendered_bytes = 0
        try:
            with _read_workbook(document) as book:
                if not book.on_demand or book.biff_version < 50:
                    raise xlrd.XLRDError("Unsupported legacy Excel workbook version")
                if book.nsheets > _MAX_SHEETS:
                    raise _limit("XLS workbook exceeds the worksheet limit")
                for index in range(book.nsheets):
                    # xlrd only exposes visibility publicly on a loaded sheet.
                    if book._sheet_visibility[index]:
                        continue
                    _check_sheet_area(book, index, min(_MAX_SHEET_AREA, _MAX_WORKBOOK_AREA - area))
                    sheet = book.sheet_by_index(index)
                    try:
                        sheet_area = sheet.nrows * sheet.ncols
                        area += sheet_area
                        if sheet_area > _MAX_SHEET_AREA or area > _MAX_WORKBOOK_AREA:
                            raise _limit("XLS workbook exceeds the worksheet area limit")
                        for block in _worksheet_batches(sheet, index, book.datemode, self.batch_size):
                            rendered_bytes += len(block.text.encode())
                            if rendered_bytes > _MAX_RENDERED_BYTES:
                                raise _limit("XLS workbook exceeds the rendered text limit")
                            yield block
                    finally:
                        book.unload_sheet(index)
                        del sheet
        except xlrd.XLRDError as exc:
            if "encrypted" in str(exc).lower():
                raise PipelineError(
                    ENCRYPTED_OFFICE_FILE_MESSAGE, code="ENCRYPTED_OFFICE_FILE", status_code=422
                ) from None
            raise PipelineError("File is not a valid XLS workbook", code="INVALID_XLS", status_code=422) from None
        except (CompDocError, OSError, ValueError, IndexError, KeyError, OverflowError, StructError, AssertionError):
            raise PipelineError("File is not a valid XLS workbook", code="INVALID_XLS", status_code=422) from None
        if rendered_bytes == 0:
            raise NoIndexableContentError("XLS contains no visible worksheet with indexable content")
