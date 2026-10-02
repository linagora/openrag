"""A CSV parser reads CSV files safely, interprets headers and rows,
handles quoted values and utf-8 with an optionnal BOM, validates the input structure,
manages missing or malformed data and converts each row into a structured format
suitable for indexing + for further processing.

Maybe it will be used later on to dealw with xlsx files converted to CSV,
but for now it is a limited CSV parser.
"""

# IMPORTS (may be updated/changed later)
import asyncio
import csv
import html
import io
from collections.abc import Iterator
from pathlib import Path

from core.indexing.parsers.document_parser import DocumentParser
from core.indexing.parsers.registry import parser_registry
from core.models.document import Document, ProcessedDocument, TextBlock
from core.utils.text import decode_bytes, detect_encoding


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
    def __init__(self, delimiter: str = ",", *, batch_size: int = 10_000):
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("the batch_size variable should only be a positive integer")
        self.delimiter = delimiter
        self.batch_size = batch_size

    def supported_types(self) -> list[str]:
        return ["csv"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(
            self._parse, document
        )  # public method used by my tests, work is given to parse to keep synchronous work off the event loop

    def _open_stream(self, document: Document):
        if document.source_path is not None:
            raw_stream = Path(document.source_path).open("rb")
            try:
                encoding = detect_encoding(raw_stream.read(64 * 1024))
                raw_stream.seek(0)
                return io.TextIOWrapper(raw_stream, encoding=encoding, errors="replace", newline="")
            except Exception:
                raw_stream.close()
                raise
        if document.text is not None:
            return io.StringIO(
                document.text.removeprefix("\ufeff"),
                newline="",
            )
        return io.StringIO(decode_bytes(document.raw_bytes or b"").removeprefix("\ufeff"), newline="")

    def iter_batches(self, document: Document) -> Iterator[TextBlock]:
        """Yield tables with at most batch_size data rows by repeating their header.

        Consuming and releasing each table before requesting the next one to save memory.
        Close this iterator if stopped early because later records can still raise errors
        after earlier batches have been yielded. (blank records are just ignored).
        """
        # opening a text stream instead of loading the entire file from disk (handling large files)
        with self._open_stream(document) as stream:
            reader = csv.reader(
                stream,
                delimiter=self.delimiter,
                strict=True,
            )

            # the generator skips blank records without storing every record
            records = (row for row in reader if row)

            # consuming the first non blank record as the header
            headers = next(records, None)

            if headers is None:
                return
            header_lines = [
                markdown_row(headers),
                markdown_row(["---"] * len(headers)),  # markdown table separator row
            ]

            # we repeat the header so each batch can be processed independently
            lines = header_lines.copy()
            rows_in_batch = 0
            batches_emitted = 0
            batch_first_record = 2

            # validate and render each record immediately
            for record_number, row in enumerate(records, start=2):
                if len(row) != len(headers):
                    raise ValueError(f"Record {record_number}: Expected {len(headers)} cells, got {len(row)}")

                lines.append(markdown_row(row))
                rows_in_batch += 1

                if rows_in_batch == self.batch_size:
                    # generate one batch and pause Until the 'caller' requests more
                    yield TextBlock(
                        text="\n".join(lines),
                        block_type="table",
                        metadata={
                            "csv_columns": list(headers),
                            "csv_row_start": batch_first_record,
                            "csv_row_end": record_number,
                        },
                    )
                    batches_emitted += 1
                    # we then start another batch without accumulating previous rows
                    lines = header_lines.copy()
                    rows_in_batch = 0
                    batch_first_record = record_number + 1

            # producing a final but partial batch or just a table containing only a header and we do not add an empty batch after an exactly full final batch
            if rows_in_batch > 0 or batches_emitted == 0:
                yield TextBlock(
                    text="\n".join(lines),
                    block_type="table",
                    metadata={
                        "csv_columns": list(headers),
                        "csv_row_start": batch_first_record,
                        "csv_row_end": batch_first_record + rows_in_batch - 1,
                    },
                )

    def _parse(self, document: Document) -> ProcessedDocument:
        # preserve the existing interface by collecting all batches
        # for lower memory usage we must use iter_batches() directly instead
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=list(
                self.iter_batches(document)
            ),  # consumes the entire generator and keeps all batches in memory inside one single ProcessedDocument
            metadata=dict(document.metadata),
        )
