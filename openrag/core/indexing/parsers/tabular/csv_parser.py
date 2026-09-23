"""A CSV parser reads CSV files safely, interprets headers and rows,
 handles quoted values and utf-8 with an optionnal BOM, validates the input structure,
 manages missing or malformed data and converts each row into a structured format
 suitable for indexing + for further processing.

 Maybe it will be used later on to dealw with xlsx files converted to CSV,
 but for now it is a limited CSV parser.
"""

# IMPORTS (may be updated later)
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
        cell = html.escape(cell, quote=False)
        cell = cell.replace("|", "&#124;") # escape pipe characters to avoid breaking the table
        cell = cell.replace("\r\n", "\n") # removing carriage returns from windows line endings
        cell = cell.replace("\r", "\n") # removing carriage returns from mac line endings
        cell = cell.replace("\n", "<br>") # replacing newlines with <br> to preserve line breaks in table cells
        escaped.append(cell)

    return "| " + " | ".join(escaped) + " |"

@parser_registry.register("csv")
class CsvParser(DocumentParser):
    def supported_types(self) -> list[str]:
        return ["csv"]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await asyncio.to_thread(self._parse, document) # public method used by my tests, work is given to parse to keep synchronous work off the event loop

    def _parse(self, document: Document) -> ProcessedDocument:
        # Get the text from the document
        if document.source_path is not None:
            text = Path(document.source_path).read_bytes().decode("utf-8-sig")
        elif document.text is not None:
            text = document.text
        else:
            text = (document.raw_bytes or b"").decode("utf-8-sig")

        text = text.removeprefix("\ufeff") #remove BOM if present using removeprefix

        # Then read CSV records (skipping blanks)
        with io.StringIO(text, newline="") as stream:
            rows = []
            for row in csv.reader(stream, delimiter=",", strict=True):
                if row: # removes empty lists but keeps empty cells
                    rows.append(row)
        blocks = []

        if rows:
            # use the first record as the header
            headers = rows[0]
            data_rows = rows[1:]

            # Check that each record has the expected width
            for row in data_rows:
                if len(row) != len(headers):
                    raise ValueError(
                        f"Expected {len(headers)} cells, got {len(row)}"
                    )

            # Render a Markdown table
            lines = [
                markdown_row(headers),
                markdown_row(["---"] * len(headers)),
            ]
            lines.extend(markdown_row(row) for row in data_rows)

            blocks.append(
                TextBlock(
                    text="\n".join(lines),
                    block_type="table",
                )
            )

        # finally return OpenRAG standard parser output
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=blocks,
            metadata=dict(document.metadata),
        )
