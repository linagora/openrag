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
from pathlib import Path

from core.indexing.parsers.document_parser import DocumentParser
from core.indexing.parsers.registry import parser_registry
from core.models.document import Document, ProcessedDocument, TextBlock


def markdown_row(cells: list[str]) -> str:
    escaped = []
    for cell in cells:
        cell = html.escape(cell, quote=False) #
        cell = cell.replace("|", "&#124;") # escape pipe characters to avoid breaking the table
        cell = cell.replace("\r\n", "\n") # removing carriage returns from windows line endings
        cell = cell.replace("\r", "\n") # removing carriage returns from mac line endings
        cell = cell.replace("\n", "<br>") # replacing newlines with <br> to preserve line breaks in table cells
        escaped.append(cell)

    return "| " + " | ".join(escaped) + " |"

@parser_registry.register("csv")
class CsvParser(DocumentParser):
    def __init__(self, delimiter: str = ","):
        self.delimiter = delimiter # stores the delimiter chosen on the parser for later

    def supported_types(self) -> list[str]:
        return ["csv"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(self._parse, document) # public method used by my tests, work is given to parse to keep synchronous work off the event loop

    def _parse(self, document: Document) -> ProcessedDocument:
        # opening a text stream instead of loading the entire file from disk (handling large files)
        if document.source_path is not None:
            stream = Path(document.source_path).open("r", encoding="utf-8-sig", newline="", ) # open the file with utf-8-sig encoding
        elif document.text is not None:
            stream = io.StringIO(document.text.removeprefix("\ufeff"), newline="", ) # remove BOM if present
        else:
            stream = io.TextIOWrapper(io.BytesIO(document.raw_bytes or b""), encoding="utf-8-sig", newline="", )

        blocks = []

        with stream:
            reader = csv.reader(
                stream,
                delimiter=self.delimiter,
                strict=True,
            )

            # the generator skips blank records without storing every record.
            records = (row for row in reader if row)

            # consuming the first nonblank record as the header.
            headers = next(records, None)

            if headers is not None:
                lines = [
                    markdown_row(headers),
                    markdown_row(["---"] * len(headers)), # markdown table separator row
                ]

                # validate and render each record immediately
                for record_number, row in enumerate(records, start=2):
                    if len(row) != len(headers):
                        raise ValueError(
                            f"Record {record_number}: "
                            f"Expected {len(headers)} cells, got {len(row)}"
                        )

                    lines.append(markdown_row(row))

                blocks.append(
                    TextBlock(
                        text="\n".join(lines),
                        block_type="table",
                        metadata={},
                    )
                )

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=blocks,
            metadata=dict(document.metadata),
        )
