"""Deterministic, readable text rendering for structured table content."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable

from core.models.document import TableCellData, TableLegendData, TableRowData

TABLE_TEXT_SERIALIZATION_VERSION = "natural-language-v1"

_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LIST_ITEM_RE = re.compile(r"^(?:[-*•]\s*|\d+(?:\.\d+)*[.)]\s+)")
_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
}


def normalize_table_text(text: str) -> str:
    """Reflow visual PDF lines while preserving semantic list boundaries."""
    value = unicodedata.normalize("NFC", html.unescape(text or "")).replace("\u00ad", "")
    value = _HTML_BREAK_RE.sub("\n", value)
    value = _HTML_TAG_RE.sub(" ", value)
    value = re.sub(r"\b([cdjlmnst])\s+'(?=\w)", r"\1'", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(le|la|les|du|des|au|aux)(?=\d)", r"\1 ", value, flags=re.IGNORECASE)
    value = re.sub(r'"\s*([^"\n]*?\S)\s*"', r'"\1"', value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)

    output: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            output.append(" ".join(paragraph))
            paragraph.clear()

    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            if output and output[-1] != "":
                output.append("")
            continue
        if _LIST_ITEM_RE.match(line):
            flush_paragraph()
            line = re.sub(r"^([-*•])(?=\S)", r"\1 ", line)
            paragraph.append(line)
        else:
            paragraph.append(line)
    flush_paragraph()

    normalized = "\n".join(output)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _quoted(value: str) -> str:
    return f"“{normalize_table_text(value)}”"


def _table_scope(
    *,
    table_title: str | None,
    section_path: list[str],
) -> str:
    section = " > ".join(part.strip() for part in section_path if part.strip())
    if table_title and section_path and section_path[-1].strip() == table_title.strip():
        parent = " > ".join(part.strip() for part in section_path[:-1] if part.strip())
        return (
            f"In section {_quoted(parent)}, table {_quoted(table_title)}"
            if parent
            else f"In table {_quoted(table_title)}"
        )
    if section and table_title:
        return f"In section {_quoted(section)}, table {_quoted(table_title)}"
    if section:
        return f"In section {_quoted(section)}, the table"
    if table_title:
        return f"In table {_quoted(table_title)}"
    return "In the table"


def _compact_table_scope(
    *,
    table_title: str | None,
    section_path: list[str],
) -> str:
    section = " > ".join(part.strip() for part in section_path if part.strip())
    if table_title and section_path and section_path[-1].strip() == table_title.strip():
        parent = " > ".join(part.strip() for part in section_path[:-1] if part.strip())
        return f"Section {_quoted(parent)}, table {_quoted(table_title)}" if parent else f"Table {_quoted(table_title)}"
    if section and table_title:
        return f"Section {_quoted(section)}, table {_quoted(table_title)}"
    if section:
        return f"Section {_quoted(section)}, table"
    if table_title:
        return f"Table {_quoted(table_title)}"
    return "Table"


def _column_names(row: TableRowData, cell: TableCellData) -> list[str]:
    end = cell.column_index + max(cell.column_span, 1)
    names = [
        candidate.column_name or f"Column {candidate.column_index + 1}"
        for candidate in row.cells
        if cell.column_index <= candidate.column_index < end
    ]
    return names or [cell.column_name or f"Column {cell.column_index + 1}"]


def _joined_quoted(values: Iterable[str]) -> str:
    quoted = [_quoted(value) for value in values]
    if len(quoted) <= 1:
        return quoted[0] if quoted else ""
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return f"{', '.join(quoted[:-1])}, and {quoted[-1]}"


def _cell_clause(row: TableRowData, cell: TableCellData) -> str | None:
    if cell.covered_by is not None:
        return None
    names = _column_names(row, cell)
    column_label = f"column {_quoted(names[0])}" if len(names) == 1 else f"columns {_joined_quoted(names)}"
    value = normalize_table_text(cell.text)
    if not value:
        return f"no value in {column_label}" if cell.explicit_empty else None
    inherited = "inherited " if cell.inherited else ""
    return f"the {inherited}value {_quoted(value)} in {column_label}"


def _join_clauses(clauses: list[str]) -> str:
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    if len(clauses) == 2:
        return f"{clauses[0]} and {clauses[1]}"
    return f"{', '.join(clauses[:-1])}, and {clauses[-1]}"


def row_reference(row: TableRowData) -> str:
    """Return a human-readable logical row reference with a stable number."""
    ordinal = _ORDINALS.get(row.row_index)
    return f"the {ordinal} row (row {row.row_index})" if ordinal else f"row {row.row_index}"


def _compact_row_reference(row: TableRowData) -> str:
    ordinal = _ORDINALS.get(row.row_index)
    return f"{ordinal} row ({row.row_index})" if ordinal else f"row {row.row_index}"


def render_table_row(
    row: TableRowData,
    *,
    cells: Iterable[TableCellData] | None = None,
) -> str:
    """Render a complete or partial row as deterministic natural language."""
    selected = list(row.cells if cells is None else cells)
    clauses = [clause for cell in selected if (clause := _cell_clause(row, cell)) is not None]
    scope = _table_scope(table_title=row.table_title, section_path=row.section_path)
    if not clauses:
        return f"{scope} contains {row_reference(row)}."
    return f"{scope}, {row_reference(row)} has {_join_clauses(clauses)}."


def render_table_row_context(
    row: TableRowData,
    *,
    cells: Iterable[TableCellData],
) -> str:
    """Render compact, self-contained identity context for split row parts."""
    selected = [
        cell for cell in cells if cell.covered_by is None and (normalize_table_text(cell.text) or cell.explicit_empty)
    ]
    scope = _compact_table_scope(table_title=row.table_title, section_path=row.section_path)
    clauses = [
        (
            f"{_quoted(cell.column_name or f'Column {cell.column_index + 1}')} = "
            f"{_quoted(cell.text) if normalize_table_text(cell.text) else 'empty'}"
        )
        for cell in selected
    ]
    if not clauses:
        return f"{scope}; {_compact_row_reference(row)}."
    return f"{scope}; {_compact_row_reference(row)}; {'; '.join(clauses)}."


def render_table_legend(legend: TableLegendData) -> str:
    """Render abbreviation definitions independently from table rows."""
    scope = _table_scope(table_title=legend.table_title, section_path=legend.section_path)
    definitions = [
        f"{entry.abbreviation} means {_quoted(entry.meaning)}"
        for entry in legend.entries
        if entry.abbreviation.strip() and entry.meaning.strip()
    ]
    if not definitions:
        return ""
    return f"{scope}, the abbreviation legend defines the following terms: {_join_clauses(definitions)}."


__all__ = [
    "TABLE_TEXT_SERIALIZATION_VERSION",
    "normalize_table_text",
    "render_table_legend",
    "render_table_row",
    "render_table_row_context",
    "row_reference",
]
