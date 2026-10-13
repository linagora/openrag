"""Streaming CSV parser that preserves headers alongside each indexed row."""

import asyncio
import codecs
import csv
import html
import io
from collections.abc import Iterator
from pathlib import Path

from core.indexing.parsers.document_parser import DocumentParser
from core.indexing.parsers.registry import parser_registry
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError
from core.utils.logging import get_logger

logger = get_logger()

_ENCODING_SAMPLE_SIZE = 64 * 1024
_DIALECT_SAMPLE_SIZE = 64 * 1024
_CSV_FIELD_SIZE_LIMIT = 16 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), _CSV_FIELD_SIZE_LIMIT))


def _detect_bytes_encoding(raw: bytes) -> str:
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            import chardet
        except ImportError:
            return "cp1252"
        return chardet.detect(raw).get("encoding") or "cp1252"
    return "utf-8-sig"


def _detect_stream_encoding(raw_stream) -> str:
    """Inspect the complete stream without retaining it in memory.

    A 64 KiB ASCII prefix is not evidence that the rest of a file is UTF-8.
    Validate UTF-8 incrementally and feed the same blocks to chardet so a late
    cp1252 character cannot silently become a replacement character.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    utf8_valid = True
    try:
        from chardet.universaldetector import UniversalDetector
    except ImportError:
        detector = None
    else:
        detector = UniversalDetector()

    raw_stream.seek(0)
    while block := raw_stream.read(_ENCODING_SAMPLE_SIZE):
        if detector is not None:
            detector.feed(block)
        if utf8_valid:
            try:
                decoder.decode(block, final=False)
            except UnicodeDecodeError:
                utf8_valid = False
    if utf8_valid:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            utf8_valid = False

    if detector is not None:
        detector.close()
    raw_stream.seek(0)
    if utf8_valid:
        return "utf-8-sig"
    encoding = (detector.result.get("encoding") if detector is not None else None) or "cp1252"
    logger.bind(encoding=encoding).warning("CSV is not valid UTF-8; using detected fallback encoding")
    return encoding


def markdown_row(cells: list[str]) -> str:
    escaped = []
    for cell in cells:
        cell = html.escape(cell, quote=False)  #
        cell = cell.replace("|", "&#124;")  # escape pipe characters to avoid breaking the table
        cell = cell.replace("\r\n", "\n")  # removing carriage returns from windows line endings
        cell = cell.replace("\r", "\n")  # removing carriage returns from mac line endings
        cell = cell.replace("\n", "<br>")  # replacing newlines with <br> to preserve line breaks in table cells
        escaped.append(cell)
    return "| " + " | ".join(escaped) + " |"


@parser_registry.register("csv")
class CsvParser(DocumentParser):
    def __init__(self, delimiter: str | None = None, *, batch_size: int = 10_000):
        if type(batch_size) is not int or batch_size < 1:
            raise ConfigError("CSV batch_size must be a positive integer")
        if delimiter is not None and (len(delimiter) != 1 or delimiter in "\r\n"):
            raise ConfigError("CSV delimiter must be one non-newline character")
        self.delimiter = delimiter
        self.batch_size = batch_size

    def supported_types(self) -> list[str]:
        return ["csv"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(self._parse, document)

    def _open_stream(self, document: Document):
        if document.source_path is not None:
            raw_stream = Path(document.source_path).open("rb")
            try:
                encoding = _detect_stream_encoding(raw_stream)
                return io.TextIOWrapper(raw_stream, encoding=encoding, errors="strict", newline="")
            except Exception:
                raw_stream.close()
                raise
        if document.text is not None:
            return io.StringIO(
                document.text.removeprefix("\ufeff"),
                newline="",
            )
        raw = document.raw_bytes or b""
        encoding = _detect_bytes_encoding(raw)
        return io.StringIO(raw.decode(encoding, errors="replace").removeprefix("\ufeff"), newline="")

    def _delimiter_for(self, stream) -> str:
        if self.delimiter is not None:
            return self.delimiter
        sample = stream.read(_DIALECT_SAMPLE_SIZE)
        stream.seek(0)
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            logger.warning("Could not detect CSV delimiter; falling back to comma")
            return ","

    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        """Yield tables with at most batch_size data rows by repeating their header.

        Consuming and releasing each table before requesting the next one to save memory.
        Close this iterator if stopped early. Blank and malformed data records
        are skipped; their cumulative count is stored in batch metadata.
        """
        with self._open_stream(document) as stream:
            reader = csv.reader(
                stream,
                delimiter=self._delimiter_for(stream),
                strict=True,
            )
            record_number = 0
            bad_rows = 0
            headers = None
            while headers is None:
                record_number += 1
                try:
                    row = next(reader)
                except StopIteration:
                    return
                except csv.Error as exc:
                    bad_rows += 1
                    logger.bind(record=record_number, line=reader.line_num).warning(
                        f"Skipping malformed CSV record: {exc}"
                    )
                    continue
                if row:
                    headers = row
            header_lines = [
                markdown_row(headers),
                markdown_row(["---"] * len(headers)),  # markdown table separator row
            ]

            # we repeat the header so each batch can be processed independently
            lines = header_lines.copy()
            rows_in_batch = 0
            batches_emitted = 0
            batch_first_record = record_number + 1
            batch_last_record = record_number
            batch_record_numbers: list[int] = []

            while True:
                record_number += 1
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    bad_rows += 1
                    logger.bind(record=record_number, line=reader.line_num).warning(
                        f"Skipping malformed CSV record: {exc}"
                    )
                    continue
                if not row:
                    continue
                if len(row) < len(headers):
                    row.extend([""] * (len(headers) - len(row)))
                elif len(row) > len(headers):
                    while len(row) > len(headers) and row[-1] == "":
                        row.pop()
                if len(row) != len(headers):
                    bad_rows += 1
                    logger.bind(
                        record=record_number,
                        expected_cells=len(headers),
                        actual_cells=len(row),
                    ).warning("Skipping malformed CSV record with the wrong number of cells")
                    continue

                if rows_in_batch == 0:
                    batch_first_record = record_number
                lines.append(markdown_row(row))
                rows_in_batch += 1
                batch_last_record = record_number
                batch_record_numbers.append(record_number)

                if rows_in_batch == self.batch_size:
                    yield TextBlock(
                        text="\n".join(lines),
                        block_type="table",
                        metadata={
                            "csv_columns": list(headers),
                            "csv_row_start": batch_first_record,
                            "csv_row_end": batch_last_record,
                            "csv_row_numbers": list(batch_record_numbers),
                            "csv_bad_rows": bad_rows,
                        },
                    )
                    batches_emitted += 1
                    lines = header_lines.copy()
                    rows_in_batch = 0
                    batch_first_record = record_number + 1
                    batch_record_numbers = []

            if rows_in_batch > 0:
                yield TextBlock(
                    text="\n".join(lines),
                    block_type="table",
                    metadata={
                        "csv_columns": list(headers),
                        "csv_row_start": batch_first_record,
                        "csv_row_end": batch_last_record,
                        "csv_row_numbers": list(batch_record_numbers),
                        "csv_bad_rows": bad_rows,
                    },
                )
            elif batches_emitted == 0:
                raise NoIndexableContentError(
                    "CSV contains a header but no indexable data rows.",
                    skipped_rows=bad_rows,
                )
            if bad_rows:
                logger.bind(skipped_rows=bad_rows).warning("CSV parsing completed with malformed records skipped")

    def _parse(self, document: Document) -> ProcessedDocument:
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=list(self.iter_batches(document)),
            metadata=dict(document.metadata),
        )
