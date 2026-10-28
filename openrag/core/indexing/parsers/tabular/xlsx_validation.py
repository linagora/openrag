"""Bound OOXML work before OpenPyXL allocates worksheet objects."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, fields
from xml.etree.ElementTree import ParseError

from core.utils.exceptions import ConfigError, PipelineError
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring, iterparse

MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CELL_REF = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
STREAMED_XML_PARTS = {"xl/sharedStrings.xml"}


@dataclass(frozen=True)
class XlsxLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_entries: int = 20_000
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_entry_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 200
    max_metadata_bytes: int = 8 * 1024 * 1024
    max_shared_strings_bytes: int = 32 * 1024 * 1024
    max_shared_strings: int = 250_000
    max_sheets: int = 128
    max_rows: int = 1_048_576
    max_columns: int = 4096
    max_cells: int = 5_000_000
    max_sheet_area: int = 5_000_000
    max_cell_chars: int = 32767
    max_batch_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ConfigError(f"XLSX {field.name} must be a positive integer")


@dataclass(frozen=True)
class SheetInfo:
    name: str
    index: int
    path: str
    hidden: bool
    rows: int
    columns: int
    formula_cells: int
    missing_formula_values: int


def invalid(message: str):
    return PipelineError(message, code="INVALID_XLSX", status_code=422)


def limit(message: str):
    return PipelineError(message, code="XLSX_LIMIT_EXCEEDED", status_code=413)


def _content_type(types, part_name: str) -> str | None:
    """Resolve a part's effective OPC content type."""
    override = next((node.get("ContentType") for node in types if node.get("PartName") == part_name), None)
    if override is not None:
        return override
    extension = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
    return next(
        (
            node.get("ContentType")
            for node in types
            if node.get("PartName") is None and node.get("Extension", "").lower() == extension
        ),
        None,
    )


def _shared_string_count(archive, types, limits):
    """Validate the bounded shared-string table and return its entry count."""
    count = 0
    paths = [
        node.get("PartName", "").lstrip("/")
        for node in types
        if node.get("ContentType", "").endswith(".sharedStrings+xml")
    ]
    if len(paths) > 1:
        raise invalid("XLSX contains multiple shared string tables")
    for path in paths:
        if archive.getinfo(path).file_size > limits.max_shared_strings_bytes:
            raise limit("XLSX shared strings exceed the memory limit")
        with archive.open(path) as stream:
            stack = []
            for event, element in iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    continue
                if element.tag.endswith("}si"):
                    texts = [node.text or "" for node in element.iter() if node.tag.endswith("}t")]
                    if sum(map(len, texts)) > limits.max_cell_chars:
                        raise limit("XLSX shared string exceeds the character limit")
                    count += 1
                    if count > limits.max_shared_strings:
                        raise limit("XLSX shared string count exceeds the memory limit")
                    if len(stack) > 1:
                        stack[-2].remove(element)
                    element.clear()
                stack.pop()
    return count


def _streamed_xml_parts(types) -> set[str]:
    """Return explicitly declared large XML parts consumed as streams."""
    suffixes = (".worksheet+xml", ".sharedStrings+xml")
    return STREAMED_XML_PARTS | {
        node.get("PartName", "").lstrip("/")
        for node in types
        if node.get("PartName") and node.get("ContentType", "").endswith(suffixes)
    }


def _is_eager_xml_part(name: str, streamed_parts: set[str]) -> bool:
    """Return whether OpenPyXL may materialize this XML package part."""
    return name.endswith((".xml", ".rels")) and name not in streamed_parts


def validate_ooxml(source, limits: XlsxLimits) -> list[SheetInfo]:
    """Inspect actual cells, not untrusted dimension/count declarations.

    Retain at most one XML row. This is an intentional extra sequential read:
    it prevents malformed dimensions from driving excessive reader work.
    Shared strings/styles are capped separately because readers load them eagerly.
    """
    try:
        if hasattr(source, "seek"):
            source.seek(0, io.SEEK_END)
            size = source.tell()
            source.seek(0)
        else:
            size = source.stat().st_size
        if size > limits.max_archive_bytes:
            raise limit("XLSX archive exceeds the compressed-size limit")
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_entries:
                raise limit("XLSX archive contains too many entries")
            total = 0
            names = set()
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or "\\" in name or ".." in name.split("/") or name in names:
                    raise invalid("XLSX archive contains an unsafe or duplicate entry path")
                if entry.flag_bits & 1:
                    raise invalid("Encrypted XLSX archives are unsupported")
                names.add(name)
                total += entry.file_size
                if entry.file_size > limits.max_entry_bytes or total > limits.max_uncompressed_bytes:
                    raise limit("XLSX archive exceeds the extraction limit")
                if entry.file_size / max(1, entry.compress_size) > limits.max_compression_ratio:
                    raise limit("XLSX archive exceeds the compression-ratio limit")
                if name.lower().endswith("vbaproject.bin"):
                    raise invalid("Macro-enabled workbooks are not accepted as XLSX")
                if name == "xl/sharedStrings.xml" and entry.file_size > limits.max_shared_strings_bytes:
                    raise limit("XLSX shared strings exceed the memory limit")

            def xml(name):
                if archive.getinfo(name).file_size > limits.max_metadata_bytes:
                    raise limit("XLSX metadata exceeds the memory limit")
                return fromstring(archive.read(name))

            types = xml("[Content_Types].xml")
            if _content_type(types, "/xl/workbook.xml") != MAIN_TYPE:
                raise invalid("OOXML package is not an XLSX workbook")
            streamed_parts = _streamed_xml_parts(types)
            eager_entries = [entry for entry in entries if _is_eager_xml_part(entry.filename, streamed_parts)]
            eager_metadata_bytes = sum(entry.file_size for entry in eager_entries)
            if any(entry.file_size > limits.max_metadata_bytes for entry in eager_entries):
                raise limit("XLSX metadata exceeds the memory limit")
            for part in types:
                if part.get("ContentType", "").endswith(".styles+xml"):
                    if archive.getinfo(part.get("PartName", "").lstrip("/")).file_size > limits.max_metadata_bytes:
                        raise limit("XLSX styles exceed the memory limit")
            shared_count = _shared_string_count(archive, types, limits)
            relationship_nodes = list(xml("xl/_rels/workbook.xml.rels"))
            relationship_ids = [relationship.get("Id") for relationship in relationship_nodes]
            if any(not rel_id for rel_id in relationship_ids) or len(set(relationship_ids)) != len(relationship_ids):
                raise invalid("XLSX workbook relationships have missing or duplicate identifiers")
            relationships = dict(zip(relationship_ids, relationship_nodes, strict=True))
            # Read-only OpenPyXL still materializes chart sheets and their
            # drawing media while opening a workbook. Bound those binary parts
            # before the backend can allocate them; ordinary worksheet media
            # remains ignored and is governed by the archive limits instead.
            if any(relationship.get("Type", "").endswith("/chartsheet") for relationship in relationship_nodes):
                eager_metadata_bytes += sum(
                    entry.file_size for entry in entries if entry.filename.startswith("xl/media/")
                )
            if eager_metadata_bytes > limits.max_metadata_bytes:
                raise limit("XLSX metadata exceeds the memory limit")
            sheets = xml("xl/workbook.xml").findall("{*}sheets/{*}sheet")
            if len(sheets) > limits.max_sheets:
                raise limit("XLSX worksheet count exceeds the limit")
            infos = []
            sheet_names = [sheet.get("name", "") for sheet in sheets]
            folded_sheet_names = [name.casefold() for name in sheet_names]
            if any(not name for name in sheet_names) or len(set(folded_sheet_names)) != len(folded_sheet_names):
                raise invalid("XLSX sheet names are missing or duplicated")
            if any(sheet.get("state", "visible") not in {"visible", "hidden", "veryHidden"} for sheet in sheets):
                raise invalid("XLSX worksheet has an invalid visibility state")
            cells_seen = 0
            worksheet_paths = set()
            for index, sheet in enumerate(sheets):
                rel_id = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
                relationship = relationships[rel_id]
                if relationship.get("TargetMode") == "External":
                    raise invalid("External worksheet relationships are unsupported")
                if not relationship.get("Type", "").endswith("/worksheet"):
                    continue
                target = relationship.get("Target", "")
                path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not path.startswith("xl/") or path not in names:
                    raise invalid("XLSX worksheet relationship has an invalid target")
                if path in worksheet_paths:
                    raise invalid("XLSX worksheet relationships contain a duplicate target")
                worksheet_paths.add(path)
                max_row = max_col = formulas = missing = 0
                previous_row = current_row = previous_col = 0
                # clear completed children so memory does not grow with row count.
                with archive.open(path) as stream:
                    stack = []
                    for event, element in iterparse(stream, events=("start", "end")):
                        if event == "start":
                            stack.append(element)
                            if element.tag.endswith("}row"):
                                current_row = int(element.get("r", "0"))
                                if current_row <= previous_row:
                                    raise invalid("XLSX row coordinates are missing or out of order")
                                if current_row > limits.max_rows:
                                    raise limit("XLSX row coordinate exceeds the row limit")
                                previous_row, previous_col = current_row, 0
                            if element.tag.endswith("}c"):
                                match = CELL_REF.fullmatch(element.get("r", ""))
                                if match is None:
                                    raise invalid("XLSX cell is missing a valid coordinate")
                                col = 0
                                for char in match[1]:
                                    col = col * 26 + ord(char) - 64
                                row = int(match[2])
                                if row > limits.max_rows or col > limits.max_columns:
                                    raise limit("XLSX cell coordinate exceeds row/column limits")
                                if row != current_row or col <= previous_col:
                                    raise invalid("XLSX cell coordinates are inconsistent or out of order")
                                previous_col = col
                                cells_seen += 1
                                if cells_seen > limits.max_cells:
                                    raise limit("XLSX cell count exceeds the limit")
                                max_row, max_col = max(max_row, row), max(max_col, col)
                            continue
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag == "c":
                            value = element.find("{*}v")
                            formula = element.find("{*}f")
                            if formula is not None:
                                formulas += 1
                                if value is None or not value.text:
                                    missing += 1
                            if sum(len(t.text or "") for t in element.iter()) > limits.max_cell_chars:
                                raise limit("XLSX cell text exceeds the character limit")
                            if element.get("t") == "s":
                                string_index = int(value.text) if value is not None and value.text else -1
                                if not 0 <= string_index < shared_count:
                                    raise invalid("XLSX cell references a missing shared string")
                        if tag in {"c", "row"} or len(stack) <= 2:
                            if len(stack) > 1:
                                stack[-2].remove(element)
                            element.clear()
                        stack.pop()
                if max_row * max_col > limits.max_sheet_area:
                    raise limit("XLSX worksheet rectangle exceeds the traversal limit")
                infos.append(
                    SheetInfo(
                        sheet.get("name", ""),
                        index,
                        path,
                        sheet.get("state", "visible") != "visible",
                        max_row,
                        max_col,
                        formulas,
                        missing,
                    )
                )
            return infos
    except (zipfile.BadZipFile, KeyError, ValueError, ParseError, DefusedXmlException, RuntimeError) as exc:
        raise invalid("File is not a valid XLSX workbook") from exc
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
