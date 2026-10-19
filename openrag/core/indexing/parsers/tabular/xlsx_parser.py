"""Streaming, sheet-aware XLSX extraction with bounded OOXML validation."""

from __future__ import annotations

import asyncio
import io
import math
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from core.indexing.parsers.registry import parser_registry
from core.indexing.parsers.tabular.base import TabularBatchParser, markdown_row, normalize_headers
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError, PipelineError
from core.utils.logging import get_logger
from defusedxml.ElementTree import fromstring

logger = get_logger()

_XLSX_MAIN_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


@dataclass(frozen=True)
class XlsxLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_entries: int = 20_000
    max_uncompressed_bytes: int = 1 * 1024 * 1024 * 1024
    max_entry_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 200


@dataclass
class _Sheet:
    name: str
    index: int
    start_row: int
    rows: Iterable[tuple[Any, ...]]


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
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return repr(value)
    return str(value)


def _validate_ooxml(source: str | Path | BinaryIO, limits: XlsxLimits) -> None:
    """Reject malformed or resource-amplifying archives before parsing."""
    if isinstance(source, (str, Path)):
        archive_size = Path(source).stat().st_size
    else:
        position = source.tell()
        source.seek(0, io.SEEK_END)
        archive_size = source.tell()
        source.seek(position)
    if archive_size > limits.max_archive_bytes:
        raise PipelineError(
            "XLSX archive exceeds the configured compressed-size limit",
            code="XLSX_LIMIT_EXCEEDED",
            status_code=413,
        )
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_entries:
                raise PipelineError(
                    "XLSX archive contains too many entries", code="XLSX_LIMIT_EXCEEDED", status_code=413
                )
            total = 0
            names = set()
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    raise PipelineError(
                        "XLSX archive contains an unsafe entry path", code="INVALID_XLSX", status_code=422
                    )
                names.add(normalized)
                total += entry.file_size
                if entry.file_size > limits.max_entry_bytes or total > limits.max_uncompressed_bytes:
                    raise PipelineError(
                        "XLSX archive exceeds the configured extraction limit",
                        code="XLSX_LIMIT_EXCEEDED",
                        status_code=413,
                    )
                if entry.file_size and entry.file_size / max(1, entry.compress_size) > limits.max_compression_ratio:
                    raise PipelineError(
                        "XLSX archive has an unsafe compression ratio",
                        code="XLSX_LIMIT_EXCEEDED",
                        status_code=413,
                    )
            required = {"[Content_Types].xml", "xl/workbook.xml"}
            if not required.issubset(names):
                raise PipelineError("File is not a valid XLSX workbook", code="INVALID_XLSX", status_code=422)
            if any(name.endswith("vbaProject.bin") for name in names):
                raise PipelineError(
                    "Macro-enabled workbooks are not accepted as XLSX", code="INVALID_XLSX", status_code=422
                )
            content_types = archive.read("[Content_Types].xml")
            root = fromstring(content_types)
            workbook_types = {
                node.attrib.get("ContentType") for node in root if node.attrib.get("PartName") == "/xl/workbook.xml"
            }
            if _XLSX_MAIN_CONTENT_TYPE not in workbook_types:
                raise PipelineError("OOXML package is not an XLSX workbook", code="INVALID_XLSX", status_code=422)
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise PipelineError("File is not a valid XLSX workbook", code="INVALID_XLSX", status_code=422) from exc
    finally:
        if hasattr(source, "seek"):
            source.seek(0)


@contextmanager
def _document_source(document: Document):
    if document.source_path is not None:
        yield Path(document.source_path)
        return
    if document.raw_bytes is None:
        raise PipelineError("XLSX parsing requires binary workbook content", code="INVALID_XLSX", status_code=422)
    stream = io.BytesIO(document.raw_bytes)
    try:
        yield stream
    finally:
        stream.close()


def _calamine_sheets(source: str | Path | BinaryIO) -> Iterator[_Sheet]:
    from python_calamine import CalamineError, CalamineWorkbook, SheetTypeEnum, SheetVisibleEnum

    workbook = None
    try:
        workbook = (
            CalamineWorkbook.from_path(source)
            if isinstance(source, (str, Path))
            else CalamineWorkbook.from_object(source)
        )
        for index, metadata in enumerate(workbook.sheets_metadata):
            if metadata.typ is not SheetTypeEnum.WorkSheet or metadata.visible is not SheetVisibleEnum.Visible:
                continue
            sheet = workbook.get_sheet_by_name(metadata.name)
            # Calamine exposes the used-range start separately, but iter_rows()
            # still includes leading empty worksheet rows. Count from Excel row 1.
            yield _Sheet(metadata.name, index, 1, sheet.iter_rows())
    except CalamineError as exc:
        raise RuntimeError(f"Calamine could not read the workbook: {exc}") from exc
    finally:
        if workbook is not None:
            workbook.close()


def _openpyxl_sheets(source: str | Path | BinaryIO) -> Iterator[_Sheet]:
    from openpyxl import load_workbook

    workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
    try:
        for index, sheet in enumerate(workbook.worksheets):
            if sheet.sheet_state != "visible":
                continue
            rows = (tuple(cell.value for cell in row) for row in sheet.iter_rows())
            yield _Sheet(sheet.title, index, 1, rows)
    finally:
        workbook.close()


@parser_registry.register("xlsx")
class XlsxParser(TabularBatchParser):
    """Extract visible worksheets as bounded canonical Markdown tables."""

    tabular_format = "xlsx"

    def __init__(
        self,
        *,
        batch_size: int = 10_000,
        backend: str = "calamine",
        fallback_backend: str | None = "openpyxl",
        limits: XlsxLimits | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ConfigError("XLSX batch_size must be a positive integer")
        allowed = {"calamine", "openpyxl"}
        if backend not in allowed or fallback_backend not in allowed | {None}:
            raise ConfigError("XLSX backend must be 'calamine' or 'openpyxl'")
        self.batch_size = batch_size
        self.backend = backend
        self.fallback_backend = fallback_backend
        self.limits = limits or XlsxLimits()

    def supported_types(self) -> list[str]:
        return ["xlsx"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(self._parse, document)

    def _backend_sheets(self, source, backend: str) -> Iterator[_Sheet]:
        return _calamine_sheets(source) if backend == "calamine" else _openpyxl_sheets(source)

    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        with _document_source(document) as source:
            _validate_ooxml(source, self.limits)
            emitted = False
            try:
                for block in self._iter_sheets(document, source, self.backend, fallback_used=False):
                    emitted = True
                    yield block
                return
            except (ImportError, RuntimeError, OSError) as exc:
                if emitted or not self.fallback_backend or self.fallback_backend == self.backend:
                    raise
                logger.bind(primary_backend=self.backend, fallback_backend=self.fallback_backend).warning(
                    f"XLSX backend failed before emitting data; using fallback: {exc}"
                )
                if hasattr(source, "seek"):
                    source.seek(0)
                yield from self._iter_sheets(document, source, self.fallback_backend, fallback_used=True)

    def _iter_sheets(self, document: Document, source, backend: str, *, fallback_used: bool) -> Iterator[TextBlock]:
        data_rows_seen = 0
        for sheet in self._backend_sheets(source, backend):
            row_iterator = iter(sheet.rows)
            header_values: list[str] | None = None
            header_row = sheet.start_row
            for physical_row, values in enumerate(row_iterator, start=sheet.start_row):
                rendered = [_cell_text(value) for value in values]
                if rendered and any(rendered):
                    header_values = normalize_headers(rendered)
                    header_row = physical_row
                    break
            if not header_values:
                continue

            header_lines = [markdown_row(header_values), markdown_row(["---"] * len(header_values))]
            lines = list(header_lines)
            row_numbers: list[int] = []
            last_column = _column_name(len(header_values))

            for physical_row, values in enumerate(row_iterator, start=header_row + 1):
                rendered = [_cell_text(value) for value in values]
                if not any(rendered):
                    continue
                if len(rendered) < len(header_values):
                    rendered.extend([""] * (len(header_values) - len(rendered)))
                while len(rendered) > len(header_values) and rendered[-1] == "":
                    rendered.pop()
                if len(rendered) > len(header_values):
                    raise PipelineError(
                        f"Worksheet {sheet.name!r}, row {physical_row} has data beyond the header width",
                        code="INVALID_XLSX_TABLE",
                        status_code=422,
                        sheet_name=sheet.name,
                        row_number=physical_row,
                    )
                lines.append(markdown_row(rendered))
                row_numbers.append(physical_row)
                data_rows_seen += 1
                if len(row_numbers) == self.batch_size:
                    yield self._block(
                        document,
                        sheet,
                        backend,
                        fallback_used,
                        header_values,
                        header_row,
                        last_column,
                        lines,
                        row_numbers,
                    )
                    lines = list(header_lines)
                    row_numbers = []
            if row_numbers:
                yield self._block(
                    document, sheet, backend, fallback_used, header_values, header_row, last_column, lines, row_numbers
                )
        if not data_rows_seen:
            raise NoIndexableContentError("XLSX contains no visible worksheet with indexable data rows")

    @staticmethod
    def _block(document, sheet, backend, fallback_used, headers, header_row, last_column, lines, row_numbers):
        table_id = f"sheet:{sheet.index}"
        cell_range = f"'{sheet.name.replace(chr(39), chr(39) * 2)}'!A{header_row}:{last_column}{row_numbers[-1]}"
        metadata = {
            "tabular_format": "xlsx",
            "workbook_filename": document.filename,
            "sheet_name": sheet.name,
            "sheet_index": sheet.index,
            "table_id": table_id,
            "table_columns": list(headers),
            "table_header_row": header_row,
            "table_row_start": row_numbers[0],
            "table_row_end": row_numbers[-1],
            "table_row_numbers": list(row_numbers),
            "cell_range": cell_range,
            "hidden_sheet": False,
            "spreadsheet_backend": backend,
            "spreadsheet_fallback_used": fallback_used,
            # Transitional aliases keep the existing table chunker operational.
            "csv_columns": list(headers),
            "csv_row_start": row_numbers[0],
            "csv_row_end": row_numbers[-1],
            "csv_row_numbers": list(row_numbers),
            "csv_bad_rows": 0,
        }
        return TextBlock(text="\n".join(lines), block_type="table", metadata=metadata)

    def _parse(self, document: Document) -> ProcessedDocument:
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=list(self.iter_batches(document)),
            metadata=dict(document.metadata),
        )
