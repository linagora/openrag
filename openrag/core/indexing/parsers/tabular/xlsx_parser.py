"""Sheet-aware XLSX extraction with bounded preflight and Markdown batches."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from core.indexing.parsers.registry import parser_registry
from core.indexing.parsers.tabular.base import TabularBatchParser, markdown_row, normalize_headers
from core.indexing.parsers.tabular.xlsx_validation import SheetInfo, TableRegion, XlsxLimits, limit, validate_ooxml
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError, PipelineError
from core.utils.logging import get_logger

logger = get_logger()


@dataclass(frozen=True)
class _FormulaValue:
    """A cached scalar paired with its inert Excel formula text."""

    cached: Any
    formula: str


def _rendered_header_key(value: str) -> str:
    """Return the canonical cell text produced by the Markdown renderer."""
    return markdown_row([value])[2:-2]


def _detect_header(rows, byte_limit, start_row=1):
    """Skip obvious title/preamble rows, retaining ambiguous first rows.

    Look ahead at most 32 physical rows and one batch worth of text. Promote a
    later row only when it is a wider textual row followed by aligned data.
    Equal-width layouts remain ambiguous and keep the first nonempty row.
    Replay buffered rows so physical coordinates remain unchanged.
    """
    buffered = []
    size = 0
    for row in rows:
        buffered.append(row)
        size += sum(len(_cell_text(v).encode("utf-8")) for v in row)
        if len(buffered) >= 32 or size >= byte_limit:
            break
    nonempty = [
        (i, row, [_cell_text(v).strip() for v in row])
        for i, row in enumerate(buffered, start_row)
        if any(_cell_text(v).strip() for v in row)
    ]
    selected = None
    if nonempty:
        selected = nonempty[0][0]
        for index, (number, raw, values) in enumerate(nonempty[1:], 1):
            labels = [value for value in values if value]
            occupied = [column for column, value in enumerate(values) if value]
            earlier = nonempty[:index]
            wider_than_preamble = all(
                sum(bool(value) for value in earlier_values) < len(occupied) for _, _, earlier_values in earlier
            )
            repeated_value_preamble = all(
                len({value for value in earlier_values if value}) == 1 for _, _, earlier_values in earlier
            )
            # Width alone is not enough evidence: a narrow first data record
            # followed by wider records is a valid table and must not lose its
            # first row. A blank physical row is the structural boundary that
            # distinguishes an optional preamble from the table below it.
            separated_from_preamble = number > nonempty[index - 1][0] + 1
            if (
                len(set(labels)) < 2
                or not all(
                    isinstance(value.cached if isinstance(value, _FormulaValue) else value, str)
                    for value in raw
                    if (value.cached if isinstance(value, _FormulaValue) else value) is not None
                )
                or not (wider_than_preamble or repeated_value_preamble)
                or not separated_from_preamble
            ):
                continue
            if index + 1 >= len(nonempty):
                break
            following = nonempty[index + 1][2]
            if all(column < len(following) and following[column] for column in occupied):
                selected = number
                break
    return selected, chain(buffered, rows)


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _worksheet_context(sheet_name: str) -> str:
    """Render the worksheet identity as searchable, single-line chunk context."""
    safe_name = sheet_name.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return f"\n\nWorksheet: {safe_name}"


def _cell_text(value: Any) -> str:
    formula = value.formula if isinstance(value, _FormulaValue) else None
    if formula is not None:
        value = value.cached
    if value is None:
        rendered = ""
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    # OpenPyXL exposes integral numbers and dates with different Python types.
    elif isinstance(value, datetime) and value.time() == time():
        rendered = value.date().isoformat()
    elif isinstance(value, (datetime, date, time)):
        rendered = value.isoformat()
    elif isinstance(value, timedelta):
        rendered = str(value)
    elif isinstance(value, float) and value.is_integer():
        rendered = str(int(value))
    else:
        rendered = str(value)
    if formula is None:
        return rendered
    if rendered:
        return f"{rendered} [formula: {formula}]"
    return f"[formula: {formula}; cached value unavailable]"


def _has_text(values: list[str]) -> bool:
    """Treat cells containing only formatting whitespace as empty."""
    return any(value.strip() for value in values)


@contextmanager
def _document_source(document: Document):
    if document.source_path is not None:
        yield Path(document.source_path)
        return
    if document.raw_bytes is None:
        raise PipelineError("XLSX parsing requires binary workbook content", code="INVALID_XLSX", status_code=422)
    with io.BytesIO(document.raw_bytes) as stream:
        yield stream


def _formula_text(cell) -> str | None:
    """Return stable formula text from regular, shared, or array formulas."""
    if getattr(cell, "data_type", None) != "f":
        return None
    value = cell.value
    text = value if isinstance(value, str) else getattr(value, "text", None)
    if not isinstance(text, str) or not text:
        return None
    return text if text.startswith("=") else f"={text}"


def _rows_with_formulas(value_rows, formula_rows):
    """Merge cached values with formula text while both readers stream."""
    for values, formula_cells in zip(value_rows, formula_rows, strict=True):
        yield tuple(
            _FormulaValue(value, formula) if (formula := _formula_text(cell)) is not None else value
            for value, cell in zip(values, formula_cells, strict=True)
        )


def _openpyxl_sheets(document: Document, source, infos: list[SheetInfo]):
    from openpyxl import load_workbook

    workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
    needs_formulas = any(info.formula_cells for info in infos if not info.hidden and info.columns)
    formula_source_manager = _document_source(document) if needs_formulas else nullcontext(None)
    formula_workbook = None
    try:
        with formula_source_manager as formula_source:
            try:
                formula_workbook = (
                    load_workbook(formula_source, read_only=True, data_only=False, keep_links=False)
                    if formula_source is not None
                    else None
                )
                for info in infos:
                    if info.hidden or not info.columns:
                        continue
                    sheet = workbook[info.name]
                    # Exporters may declare A1:A1 or a huge dimension. Use inspected cells.
                    sheet.reset_dimensions()
                    for table in info.tables:
                        with closing(
                            sheet.iter_rows(
                                min_row=table.start_row,
                                max_row=table.end_row,
                                min_col=table.start_column,
                                max_col=table.end_column,
                                values_only=True,
                            )
                        ) as rows:
                            if not info.formula_cells:
                                yield info, table, rows
                                continue
                            if formula_workbook is None:
                                raise PipelineError("XLSX formula reader is unavailable", code="INVALID_XLSX")
                            formula_sheet = formula_workbook[info.name]
                            formula_sheet.reset_dimensions()
                            with closing(
                                formula_sheet.iter_rows(
                                    min_row=table.start_row,
                                    max_row=table.end_row,
                                    min_col=table.start_column,
                                    max_col=table.end_column,
                                    values_only=False,
                                )
                            ) as formula_rows:
                                yield info, table, _rows_with_formulas(rows, formula_rows)
            finally:
                if formula_workbook is not None:
                    formula_workbook.close()
    finally:
        workbook.close()


@parser_registry.register("xlsx")
class XlsxParser(TabularBatchParser):
    """Extract visible worksheets; limits bound accepted input, not exact RSS."""

    tabular_format = "xlsx"

    def __init__(
        self,
        *,
        batch_size: int = 10_000,
        header_row: int | None = None,
        sheet_header_rows: dict[str, int | None] | None = None,
        limits: XlsxLimits | dict[str, int] | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ConfigError("XLSX batch_size must be a positive integer")
        if header_row is not None and (type(header_row) is not int or not 1 <= header_row <= 1_048_576):
            raise ConfigError("XLSX header_row must be an Excel row number between 1 and 1048576")
        self.header_row = header_row
        if sheet_header_rows is not None and not isinstance(sheet_header_rows, dict):
            raise ConfigError("XLSX sheet_header_rows must map worksheet names to row numbers or null")
        for name, row in (sheet_header_rows or {}).items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("XLSX sheet_header_rows requires nonempty worksheet names")
            if row is not None and (type(row) is not int or not 1 <= row <= 1_048_576):
                raise ConfigError("XLSX sheet header rows must be Excel row numbers between 1 and 1048576 or null")
        self.sheet_header_rows = dict(sheet_header_rows or {})
        self.batch_size = batch_size
        try:
            self.limits = XlsxLimits(**limits) if isinstance(limits, dict) else limits or XlsxLimits()
        except TypeError as exc:
            raise ConfigError(f"Invalid XLSX limits: {exc}") from exc

    def supported_types(self) -> list[str]:
        return ["xlsx"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(self._parse, document)

    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        from openpyxl.utils.exceptions import InvalidFileException

        with _document_source(document) as source:
            infos = validate_ooxml(source, self.limits)
            # Validate overrides before opening a backend or emitting any batches.
            by_name = {info.name: info for info in infos}
            if self.header_row is not None:
                visible = [info for info in infos if not info.hidden and info.columns]
                for info in visible:
                    if self.header_row > info.rows:
                        raise PipelineError(
                            f"XLSX sheet {info.name!r}: selected header row {self.header_row} is missing",
                            code="INVALID_XLSX",
                            status_code=422,
                        )
                    if not any(table.start_row <= self.header_row <= table.end_row for table in info.tables):
                        raise PipelineError(
                            f"XLSX sheet {info.name!r}: selected header row {self.header_row} is empty",
                            code="INVALID_XLSX",
                            status_code=422,
                        )
            for name, row in self.sheet_header_rows.items():
                info = by_name.get(name)
                if info is None or info.hidden or not info.columns:
                    raise PipelineError(
                        f"XLSX header override targets an absent, hidden, or empty worksheet: {name!r}",
                        code="INVALID_XLSX",
                        status_code=422,
                    )
                if row is not None and row > info.rows:
                    raise PipelineError(
                        f"XLSX sheet {name!r}: selected header row {row} is missing",
                        code="INVALID_XLSX",
                        status_code=422,
                    )
                if row is not None and not any(table.start_row <= row <= table.end_row for table in info.tables):
                    raise PipelineError(
                        f"XLSX sheet {name!r}: selected header row {row} is empty",
                        code="INVALID_XLSX",
                        status_code=422,
                    )
            with closing(self._iter_sheets(document, source, infos)) as batches:
                try:
                    yield from batches
                except PipelineError:
                    raise
                except (
                    InvalidFileException,
                    OSError,
                    KeyError,
                    IndexError,
                    TypeError,
                    ValueError,
                    SyntaxError,
                    zipfile.BadZipFile,
                ) as exc:
                    raise PipelineError(
                        "File is not a valid XLSX workbook", code="INVALID_XLSX", status_code=422
                    ) from exc

    def _iter_sheets(self, document, source, infos):
        data_rows_seen = 0
        hidden_count = sum(info.hidden for info in infos)
        with closing(_openpyxl_sheets(document, source, infos)) as sheets:
            for sheet, table, row_iterator in sheets:
                configured_header_row = self.sheet_header_rows.get(sheet.name, self.header_row)
                selected_header_row = (
                    configured_header_row
                    if configured_header_row is not None and table.start_row <= configured_header_row <= table.end_row
                    else None
                )
                if configured_header_row is None and table.header_row_hint is not None:
                    selected_header_row = table.header_row_hint
                if selected_header_row is None:
                    selected_header_row, row_iterator = _detect_header(
                        row_iterator, self.limits.max_batch_bytes, table.start_row
                    )
                preamble = []
                preamble_rows = []
                preamble_bytes = 0
                header_values = None
                header_row = table.start_row
                for physical_row, values in enumerate(row_iterator, start=table.start_row):
                    if selected_header_row is not None and physical_row < selected_header_row:
                        # Repeated merged-title values are one piece of context.
                        values_text = list(
                            dict.fromkeys(_cell_text(v).strip() for v in values if _cell_text(v).strip())
                        )
                        if values_text:
                            text = " ; ".join(values_text)
                            preamble_bytes += len(text.encode("utf-8"))
                            if preamble_bytes >= self.limits.max_batch_bytes:
                                raise limit("XLSX sheet context exceeds the batch byte limit")
                            preamble.append(f"Row {physical_row}: {text}")
                            preamble_rows.append(physical_row)
                        continue
                    rendered = [_cell_text(value) for value in values]
                    if selected_header_row is not None and not _has_text(rendered):
                        raise PipelineError(
                            f"XLSX sheet {sheet.name!r}: selected header row {selected_header_row} is empty",
                            code="INVALID_XLSX",
                            status_code=422,
                        )
                    if _has_text(rendered):
                        rendered.extend([""] * (table.end_column - table.start_column + 1 - len(rendered)))
                        if any(len(value) > self.limits.max_cell_chars for value in rendered):
                            raise limit("XLSX header text exceeds the character limit")
                        if sum(len(value.encode("utf-8")) for value in rendered) >= self.limits.max_batch_bytes:
                            raise limit("XLSX header exceeds the batch byte limit")
                        header_values = normalize_headers([value.strip() for value in rendered])
                        # Column identities must remain unique after Markdown
                        # normalizes whitespace and line endings.
                        reserved = {_rendered_header_key(name) for name in header_values}
                        used = set()
                        for index, name in enumerate(header_values):
                            candidate, suffix = name, 2
                            candidate_key = _rendered_header_key(candidate)
                            if candidate_key in used:
                                candidate = f"{name}_{suffix}"
                                candidate_key = _rendered_header_key(candidate)
                                while candidate_key in reserved or candidate_key in used:
                                    suffix += 1
                                    candidate = f"{name}_{suffix}"
                                    candidate_key = _rendered_header_key(candidate)
                            header_values[index] = candidate
                            used.add(candidate_key)
                        header_row = physical_row
                        break
                if not header_values:
                    if selected_header_row is not None:
                        raise PipelineError(
                            f"XLSX sheet {sheet.name!r}: selected header row {selected_header_row} is missing",
                            code="INVALID_XLSX",
                            status_code=422,
                        )
                    continue
                if sheet.missing_formula_values:
                    logger.bind(sheet_name=sheet.name, missing_formula_values=sheet.missing_formula_values).warning(
                        "XLSX formula text was indexed without a cached result; formulas are not recalculated"
                    )
                header_lines = [markdown_row(header_values), markdown_row(["---"] * len(header_values))]
                worksheet_context = _worksheet_context(sheet.name)
                sheet_context = (
                    "\n\nWorksheet context (before column headers):\n" + "\n".join(preamble) if preamble else ""
                )
                context = worksheet_context + sheet_context
                header_bytes = len(("\n".join(header_lines) + context).encode("utf-8"))
                if header_bytes >= self.limits.max_batch_bytes:
                    raise limit("XLSX header exceeds the batch byte limit")
                lines, row_numbers, batch_bytes = list(header_lines), [], header_bytes

                def block():
                    return self._block(
                        document,
                        sheet,
                        table,
                        hidden_count,
                        header_values,
                        header_row,
                        lines,
                        row_numbers,
                        worksheet_context,
                        sheet_context,
                        preamble_rows,
                    )

                for physical_row, values in enumerate(row_iterator, start=header_row + 1):
                    rendered = [_cell_text(value) for value in values]
                    if not _has_text(rendered):
                        continue
                    if any(len(value) > self.limits.max_cell_chars for value in rendered):
                        raise limit("XLSX cell text exceeds the character limit")
                    rendered.extend([""] * (len(header_values) - len(rendered)))
                    if len(rendered) > len(header_values):
                        raise PipelineError("XLSX reader returned more columns than inspected", code="INVALID_XLSX")
                    if (
                        header_bytes + sum(len(value.encode("utf-8")) for value in rendered)
                        >= self.limits.max_batch_bytes
                    ):
                        raise limit(f"XLSX row {physical_row} exceeds the batch byte limit")
                    row = markdown_row(rendered)
                    row_bytes = len(row.encode("utf-8")) + 1
                    if header_bytes + row_bytes > self.limits.max_batch_bytes:
                        raise limit(f"XLSX row {physical_row} exceeds the batch byte limit")
                    if row_numbers and batch_bytes + row_bytes > self.limits.max_batch_bytes:
                        yield block()
                        lines, row_numbers, batch_bytes = list(header_lines), [], header_bytes
                    lines.append(row)
                    row_numbers.append(physical_row)
                    batch_bytes += row_bytes
                    data_rows_seen += 1
                    if len(row_numbers) == self.batch_size:
                        yield block()
                        lines, row_numbers, batch_bytes = list(header_lines), [], header_bytes
                if row_numbers:
                    yield block()
        if not data_rows_seen:
            raise NoIndexableContentError("XLSX contains no visible worksheet with indexable data rows")

    def _block(
        self,
        document,
        sheet,
        table: TableRegion,
        hidden_count,
        headers,
        header_row,
        lines,
        row_numbers,
        worksheet_context,
        sheet_context,
        preamble_rows,
    ):
        sheet_ref = "'" + sheet.name.replace("'", "''") + "'!"
        first_column = _column_name(table.start_column)
        last_column = _column_name(table.end_column)
        metadata = {
            "tabular_format": "xlsx",
            "workbook_filename": document.filename,
            "sheet_name": sheet.name,
            "worksheet_context": worksheet_context,
            "sheet_index": sheet.index,
            "table_id": f"sheet:{sheet.index}:table:{table.index}",
            "table_index": table.index,
            "worksheet_table_count": len(sheet.tables),
            "table_columns": list(headers),
            "table_header_row": header_row,
            "table_start_row": header_row,
            "table_end_row": table.end_row,
            "table_start_column": table.start_column,
            "table_end_column": table.end_column,
            "table_range": f"{sheet_ref}{first_column}{header_row}:{last_column}{table.end_row}",
            "table_region_range": f"{sheet_ref}{first_column}{table.start_row}:{last_column}{table.end_row}",
            "table_region_start_row": table.start_row,
            "table_row_start": row_numbers[0],
            "table_row_end": row_numbers[-1],
            "table_row_numbers": list(row_numbers),
            "cell_range": f"{sheet_ref}{first_column}{row_numbers[0]}:{last_column}{row_numbers[-1]}",
            "table_header_range": f"{sheet_ref}{first_column}{header_row}:{last_column}{header_row}",
            "hidden_sheet": False,
            "hidden_sheets_skipped": hidden_count,
            "formula_policy": "cached_values_and_formula_text",
            "sheet_formula_cells": sheet.formula_cells,
            "sheet_missing_formula_values": sheet.missing_formula_values,
            "spreadsheet_backend": "openpyxl",
            # Transitional aliases keep existing chunking/retrieval operational.
            "csv_columns": list(headers),
            "csv_row_start": row_numbers[0],
            "csv_row_end": row_numbers[-1],
            "csv_row_numbers": list(row_numbers),
            "csv_bad_rows": 0,
        }
        if sheet_context:
            metadata.update(sheet_context=sheet_context, sheet_context_row_numbers=list(preamble_rows))
        return TextBlock(
            text="\n".join(lines) + worksheet_context + sheet_context,
            block_type="table",
            metadata=metadata,
        )

    def _parse(self, document: Document) -> ProcessedDocument:
        # The compatibility API collects blocks; production ingestion uses iter_batches.
        return ProcessedDocument(
            document_id=document.id, text_blocks=list(self.iter_batches(document)), metadata=dict(document.metadata)
        )
