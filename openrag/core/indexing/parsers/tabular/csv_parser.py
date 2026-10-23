"""Streaming CSV parser that preserves headers alongside each indexed row."""

import asyncio
import codecs
import csv
import html
import io
import re
from collections.abc import Iterator
from pathlib import Path

from core.indexing.parsers.document_parser import DocumentParser
from core.indexing.parsers.registry import parser_registry
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.exceptions import ConfigError, NoIndexableContentError
from core.utils.logging import get_logger

logger = get_logger()

_ENCODING_SAMPLE_SIZE = 64 * 1024
_ENCODING_DETECTION_LIMIT = 4096
_INVALID_UTF8 = re.compile("[\udc80-\udcff]")
_VALID_MULTIBYTE = re.compile("[\u0080-\ud7ff\ue000-\U0010ffff]")
_NON_ASCII_BYTE = re.compile(b"[\x80-\xff]")
_LONG_ASCII_RUN = re.compile(rb"[\x00-\x7f]{129,}")
_DIALECT_SAMPLE_SIZE = 64 * 1024
_CSV_FIELD_SIZE_LIMIT = 16 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), _CSV_FIELD_SIZE_LIMIT))


def _detect_stream_encoding(raw_stream) -> tuple[str, str]:
    """Inspect the complete stream without retaining it in memory.

    A 64 KiB ASCII prefix is not evidence that the rest of a file is UTF-8.
    Validate UTF-8 incrementally first. Only if validation fails, run chardet
    on a small sample around non-ASCII bytes. Prefer tolerant UTF-8 only when
    valid multibyte characters outnumber invalid bytes.
    """
    # ``surrogateescape`` lets validation continue after an invalid byte while
    # keeping invalid bytes distinguishable from valid Unicode characters.
    # Stopping at the first error would miss UTF-8 multibyte text later in the
    # file and let chardet silently reinterpret that text as a single-byte
    # encoding.
    decoder = codecs.getincrementaldecoder("utf-8-sig")("surrogateescape")
    invalid_bytes = 0
    multibyte_characters = 0
    detection_sample = b""
    previous_tail = b""

    raw_stream.seek(0)
    while block := raw_stream.read(_ENCODING_SAMPLE_SIZE):
        decoded = decoder.decode(block, final=False)
        if not decoded.isascii():
            invalid_bytes += len(_INVALID_UTF8.findall(decoded))
            multibyte_characters += len(_VALID_MULTIBYTE.findall(decoded))
            if not detection_sample and invalid_bytes:
                candidate = previous_tail + block
                first_non_ascii = _NON_ASCII_BYTE.search(candidate)
                if first_non_ascii is not None:
                    start = max(0, first_non_ascii.start() - 64)
                    detection_sample = candidate[start : start + _ENCODING_DETECTION_LIMIT]
        previous_tail = block[-4:]

    decoded = decoder.decode(b"", final=True)
    invalid_bytes += len(_INVALID_UTF8.findall(decoded))
    multibyte_characters += len(_VALID_MULTIBYTE.findall(decoded))
    if invalid_bytes and not detection_sample:
        detection_sample = previous_tail

    raw_stream.seek(0)
    if not invalid_bytes:
        return "utf-8-sig", "strict"

    # Require a strict majority: this keeps small UTF-8 files with one stray
    # byte intact without letting tied or sparse coincidental UTF-8 sequences
    # reinterpret a single-byte encoded file.
    if multibyte_characters > invalid_bytes:
        logger.warning("CSV is predominantly UTF-8 with invalid bytes; replacing undecodable bytes")
        return "utf-8-sig", "replace"

    # Long ASCII words provide no encoding evidence and can make chardet's
    # language probers disproportionately expensive. Keep nearby context only.
    detection_sample = _LONG_ASCII_RUN.sub(lambda match: match[0][:64] + b" " + match[0][-64:], detection_sample)
    try:
        import chardet
    except ImportError:
        encoding = "cp1252"
    else:
        detected = chardet.detect(detection_sample)
        encoding = detected.get("encoding") or "cp1252"
        if detected.get("confidence", 1.0) < 0.2:
            # Tiny or repetitive samples cannot reliably identify a language.
            # Use the same fallback as an unavailable detector.
            encoding = "cp1252"

    normalized_encoding = encoding.lower().replace("_", "-")
    if normalized_encoding in {"iso-8859-1", "latin-1", "latin1"}:
        encoding = "cp1252"
    if normalized_encoding in {"ascii", "utf-8", "utf-8-sig"}:
        logger.bind(encoding=encoding).warning("CSV contains invalid UTF-8 bytes; replacing undecodable bytes")
        return "utf-8-sig", "replace"
    logger.bind(encoding=encoding).warning("CSV is not valid UTF-8; using detected fallback encoding")
    return encoding, "replace"


def _detect_bytes_encoding(raw: bytes) -> tuple[str, str]:
    return _detect_stream_encoding(io.BytesIO(raw))


def _normalize_headers(headers: list[str]) -> list[str]:
    """Give blank headers stable names without colliding with explicit names."""
    used_names = {header for header in headers if header}
    normalized = []
    for index, header in enumerate(headers, start=1):
        if header:
            normalized.append(header)
            continue
        base_name = f"column_{index}"
        generated_name = base_name
        suffix = 2
        while generated_name in used_names:
            generated_name = f"{base_name}_{suffix}"
            suffix += 1
        normalized.append(generated_name)
        used_names.add(generated_name)
    return normalized


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
                encoding, errors = _detect_stream_encoding(raw_stream)
                return io.TextIOWrapper(raw_stream, encoding=encoding, errors=errors, newline="")
            except Exception:
                raw_stream.close()
                raise
        if document.text is not None:
            return io.StringIO(
                document.text.removeprefix("\ufeff"),
                newline="",
            )
        raw = document.raw_bytes or b""
        encoding, errors = _detect_bytes_encoding(raw)
        return io.StringIO(raw.decode(encoding, errors=errors).removeprefix("\ufeff"), newline="")

    def _delimiter_for(self, stream) -> str:
        if self.delimiter is not None:
            return self.delimiter
        sample = stream.read(_DIALECT_SAMPLE_SIZE)
        stream.seek(0)
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            # A common spreadsheet export leaves an extra delimiter at the
            # end of every data row. That gives the header and data records
            # different widths, which prevents ``Sniffer`` from identifying
            # an otherwise unambiguous dialect. Retry with only line-ending
            # candidate delimiters removed; the original stream is untouched.
            normalized_sample = "\n".join(line.rstrip(",;\t|") for line in sample.splitlines())
            try:
                return csv.Sniffer().sniff(normalized_sample, delimiters=",;\t|").delimiter
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
            decode_replacements = 0
            last_parse_error: str | None = None
            headers = None
            while headers is None:
                record_number += 1
                previous_line_number = reader.line_num
                try:
                    row = next(reader)
                except StopIteration:
                    if bad_rows:
                        detail = f" {last_parse_error}" if last_parse_error else ""
                        raise NoIndexableContentError(
                            f"CSV contains no valid header row.{detail}",
                            skipped_rows=bad_rows,
                        )
                    return
                except csv.Error as exc:
                    consumed_lines = max(1, reader.line_num - previous_line_number)
                    bad_rows += consumed_lines
                    record_number += consumed_lines - 1
                    last_parse_error = (
                        f"Malformed record starting at physical line {previous_line_number + 1} "
                        f"consumed {consumed_lines} physical line(s): {exc}."
                    )
                    logger.bind(
                        record=record_number,
                        start_line=previous_line_number + 1,
                        end_line=reader.line_num,
                        skipped_lines=consumed_lines,
                    ).warning(last_parse_error)
                    continue
                if row:
                    headers = row
            headers = _normalize_headers(headers)
            decode_replacements += sum(header.count("\ufffd") for header in headers)
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
                previous_line_number = reader.line_num
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    consumed_lines = max(1, reader.line_num - previous_line_number)
                    bad_rows += consumed_lines
                    record_number += consumed_lines - 1
                    last_parse_error = (
                        f"Malformed record starting at physical line {previous_line_number + 1} "
                        f"consumed {consumed_lines} physical line(s): {exc}."
                    )
                    logger.bind(
                        record=record_number,
                        start_line=previous_line_number + 1,
                        end_line=reader.line_num,
                        skipped_lines=consumed_lines,
                    ).warning(last_parse_error)
                    continue
                if not row:
                    continue
                decode_replacements += sum(cell.count("\ufffd") for cell in row)
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
                            "csv_decode_replacements": decode_replacements,
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
                        "csv_decode_replacements": decode_replacements,
                    },
                )
            elif batches_emitted == 0:
                detail = f" {last_parse_error}" if last_parse_error else ""
                raise NoIndexableContentError(
                    f"CSV contains a header but no indexable data rows.{detail}",
                    skipped_rows=bad_rows,
                )
            if bad_rows:
                logger.bind(skipped_rows=bad_rows).warning("CSV parsing completed with malformed records skipped")
            if decode_replacements:
                logger.bind(replacements=decode_replacements).warning(
                    "CSV parsing completed with undecodable bytes replaced"
                )

    def _parse(self, document: Document) -> ProcessedDocument:
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=list(self.iter_batches(document)),
            metadata=dict(document.metadata),
        )
