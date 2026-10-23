"""Sheet-aware XLSX extraction with bounded preflight and Markdown batches."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from core.indexing.parsers.registry import parser_registry
from core.indexing.parsers.tabular.base import TabularBatchParser, markdown_row, normalize_headers
from core.indexing.parsers.tabular.xlsx_validation import SheetInfo, XlsxLimits, limit, validate_ooxml
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError, PipelineError
from core.utils.logging import get_logger

logger = get_logger()


def _rendered_header_key(value: str) -> str:
    """Return the canonical cell text produced by the Markdown renderer."""
    return markdown_row([value])[2:-2]


def _detect_header(rows, byte_limit):
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
        for i, row in enumerate(buffered, 1)
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
            separated_repeated_title = number > nonempty[index - 1][0] + 1 and all(
                len({value for value in earlier_values if value}) == 1 for _, _, earlier_values in earlier
            )
            if (
                len(set(labels)) < 2
                or not all(isinstance(value, str) for value in raw if value is not None)
                or not (wider_than_preamble or separated_repeated_title)
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


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    # Both engines expose integral numbers/dates with different Python types.
    if isinstance(value, datetime) and value.time() == time():
        return value.date().isoformat()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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


def _openpyxl_sheets(source, infos: list[SheetInfo]):
    from openpyxl import load_workbook

    workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
    try:
        for info in infos:
            if info.hidden or not info.columns:
                continue
            sheet = workbook[info.name]
            # Exporters may declare A1:A1 or a huge dimension. Use inspected cells.
            sheet.reset_dimensions()
            with closing(sheet.iter_rows(max_row=info.rows, max_col=info.columns, values_only=True)) as rows:
                yield info, rows
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
        with _document_source(document) as source:
            infos = validate_ooxml(source, self.limits)
            # Validate overrides before opening a backend or emitting any batches.
            by_name = {info.name: info for info in infos}
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
            with closing(self._iter_sheets(document, source, infos)) as batches:
                yield from batches

    def _iter_sheets(self, document, source, infos):
        data_rows_seen = 0
        hidden_count = sum(info.hidden for info in infos)
        with closing(_openpyxl_sheets(source, infos)) as sheets:
            for sheet, row_iterator in sheets:
                selected_header_row = self.sheet_header_rows.get(sheet.name, self.header_row)
                if selected_header_row is None:
                    selected_header_row, row_iterator = _detect_header(row_iterator, self.limits.max_batch_bytes)
                preamble = []
                preamble_rows = []
                preamble_bytes = 0
                header_values = None
                header_row = 1
                for physical_row, values in enumerate(row_iterator, start=1):
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
                        rendered.extend([""] * (sheet.columns - len(rendered)))
                        if any(len(value) > self.limits.max_cell_chars for value in rendered):
                            raise limit("XLSX header text exceeds the character limit")
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
                        "XLSX formulas without cached values are empty; formulas are not recalculated"
                    )
                header_lines = [markdown_row(header_values), markdown_row(["---"] * len(header_values))]
                context = "\n\nWorksheet context (before column headers):\n" + "\n".join(preamble) if preamble else ""
                header_bytes = len(("\n".join(header_lines) + context).encode("utf-8"))
                if header_bytes >= self.limits.max_batch_bytes:
                    raise limit("XLSX header exceeds the batch byte limit")
                lines, row_numbers, batch_bytes = list(header_lines), [], header_bytes

                def block():
                    return self._block(
                        document,
                        sheet,
                        hidden_count,
                        header_values,
                        header_row,
                        lines,
                        row_numbers,
                        context,
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
        hidden_count,
        headers,
        header_row,
        lines,
        row_numbers,
        context,
        preamble_rows,
    ):
        sheet_ref = "'" + sheet.name.replace("'", "''") + "'!"
        last_column = _column_name(len(headers))
        metadata = {
            "tabular_format": "xlsx",
            "workbook_filename": document.filename,
            "sheet_name": sheet.name,
            "sheet_index": sheet.index,
            "table_id": f"sheet:{sheet.index}",
            "table_columns": list(headers),
            "table_header_row": header_row,
            "table_row_start": row_numbers[0],
            "table_row_end": row_numbers[-1],
            "table_row_numbers": list(row_numbers),
            "cell_range": f"{sheet_ref}A{row_numbers[0]}:{last_column}{row_numbers[-1]}",
            "table_header_range": f"{sheet_ref}A{header_row}:{last_column}{header_row}",
            "hidden_sheet": False,
            "hidden_sheets_skipped": hidden_count,
            "formula_policy": "cached_values_only",
            "sheet_missing_formula_values": sheet.missing_formula_values,
            "spreadsheet_backend": "openpyxl",
            # Transitional aliases keep existing chunking/retrieval operational.
            "csv_columns": list(headers),
            "csv_row_start": row_numbers[0],
            "csv_row_end": row_numbers[-1],
            "csv_row_numbers": list(row_numbers),
            "csv_bad_rows": 0,
        }
        if context:
            metadata.update(sheet_context=context, sheet_context_row_numbers=list(preamble_rows))
        return TextBlock(text="\n".join(lines) + context, block_type="table", metadata=metadata)

    def _parse(self, document: Document) -> ProcessedDocument:
        # The compatibility API collects blocks; production ingestion uses iter_batches.
        return ProcessedDocument(
            document_id=document.id, text_blocks=list(self.iter_batches(document)), metadata=dict(document.metadata)
        )
