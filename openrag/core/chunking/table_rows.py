"""Self-contained chunking for normalized logical table rows."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.indexing.table_text import (
    TABLE_TEXT_SERIALIZATION_VERSION,
    normalize_table_text,
    render_table_legend,
    render_table_row,
    render_table_row_context,
)
from core.models.document import (
    SourceFragment,
    TableCellData,
    TableLegendData,
    TableRowData,
)

_SEMANTIC_BOUNDARY_RE = re.compile(r"\n{2,}|(?=\n\s*(?:#{1,6}\s+|[-*•]\s+|\d+(?:[.)]|\.\d+[.)]?)\s+))|(?<=[.!?;:])\s+")


@dataclass(slots=True, frozen=True)
class TableRowChunk:
    text: str
    page_number: int
    metadata: dict[str, Any]


def _identity_cells(row: TableRowData, content_columns: set[int]) -> list[TableCellData]:
    return [
        cell
        for cell in row.cells
        if cell.column_index not in content_columns
        and cell.covered_by is None
        and (cell.text.strip() or cell.explicit_empty)
    ]


def _full_row_text(row: TableRowData) -> str:
    return render_table_row(row)


def _compose_chunk(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _select_repeated_context(
    row: TableRowData,
    candidates: list[TableCellData],
    *,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> tuple[str, list[TableCellData]]:
    """Keep only complete identity clauses that leave room for row content."""
    reserve = "Column 999999 (999999/999999):\nx"
    full_scope = render_table_row_context(row, cells=[])
    if length_function(_compose_chunk(full_scope, reserve)) > chunk_size:
        minimal_scope = f"Row {row.row_index}."
        return (
            minimal_scope if length_function(_compose_chunk(minimal_scope, reserve)) <= chunk_size else "",
            [],
        )

    selected: list[TableCellData] = []
    prefix = full_scope
    for cell in candidates:
        candidate = render_table_row_context(row, cells=[*selected, cell])
        if length_function(_compose_chunk(candidate, reserve)) <= chunk_size:
            selected.append(cell)
            prefix = candidate
    return prefix, selected


def _select_heading(
    *,
    prefix: str,
    cell: TableCellData,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> str:
    label = normalize_table_text(cell.column_name or f"Column {cell.column_index + 1}")
    options = (
        f"Column “{label}” (999999/999999):",
        f"Column {cell.column_index + 1} (999999/999999):",
        "Value (999999/999999):",
        "",
    )
    for heading in options:
        if length_function(_compose_chunk(prefix, heading, "x")) <= chunk_size:
            return heading
    return ""


def _part_heading(template: str, part: int, total: int) -> str:
    return template.replace("999999/999999", f"{part}/{total}")


def _hard_split(
    text: str,
    start: int,
    budget: int,
    length_function: Callable[[str], int],
) -> list[tuple[str, int, int]]:
    pieces: list[tuple[str, int, int]] = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        if length_function(remaining) <= budget:
            pieces.append((remaining.strip(), start + cursor, start + len(text)))
            break
        low, high = 1, len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if length_function(remaining[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        cut = max(1, low)
        whitespace = remaining.rfind(" ", 0, cut + 1)
        if whitespace > 0:
            cut = whitespace
        piece = remaining[:cut].strip()
        if piece:
            leading = len(remaining[:cut]) - len(remaining[:cut].lstrip())
            pieces.append((piece, start + cursor + leading, start + cursor + cut))
        cursor += cut
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return pieces


def _semantic_split(
    text: str,
    budget: int,
    length_function: Callable[[str], int],
    *,
    hard_boundaries: set[int] | None = None,
) -> list[tuple[str, int, int]]:
    if not text:
        return []
    boundaries = [0]
    boundaries.extend(match.end() for match in _SEMANTIC_BOUNDARY_RE.finditer(text))
    boundaries.extend(hard_boundaries or ())
    boundaries.append(len(text))
    boundaries = sorted(set(boundaries))
    units = [
        (text[start:end].strip(), start, end)
        for start, end in zip(boundaries, boundaries[1:], strict=False)
        if text[start:end].strip()
    ]

    pieces: list[tuple[str, int, int]] = []
    current_text = ""
    current_start = 0
    current_end = 0
    for unit, start, end in units:
        if current_text and hard_boundaries and start in hard_boundaries:
            pieces.append((current_text, current_start, current_end))
            current_text = ""
        candidate = f"{current_text}\n\n{unit}".strip() if current_text else unit
        if current_text and length_function(candidate) > budget:
            pieces.append((current_text, current_start, current_end))
            current_text = ""
        if length_function(unit) > budget:
            pieces.extend(_hard_split(unit, start, budget, length_function))
            continue
        if not current_text:
            current_text = unit
            current_start = start
        else:
            current_text = f"{current_text}\n\n{unit}"
        current_end = end
    if current_text:
        pieces.append((current_text, current_start, current_end))
    return pieces


def _overlapping_fragments(
    fragments: list[SourceFragment],
    start: int,
    end: int,
) -> list[SourceFragment]:
    return [fragment for fragment in fragments if (fragment.text_end or 0) > start and fragment.text_start < end]


def _dump_fragments(fragments: list[SourceFragment]) -> list[dict[str, Any]]:
    return [fragment.model_dump(mode="json") for fragment in fragments]


def _all_row_fragments(row: TableRowData) -> list[SourceFragment]:
    return [
        *row.scope_fragments,
        *(fragment for cell in row.cells for fragment in cell.source_fragments),
    ]


def _piece_page_number(
    fragments: list[SourceFragment],
    start: int,
    end: int,
    *,
    fallback: int,
) -> int:
    overlapping = _overlapping_fragments(fragments, start, end)
    if not overlapping:
        return fallback
    return max(
        overlapping,
        key=lambda fragment: (
            min(end, fragment.text_end or end) - max(start, fragment.text_start),
            -fragment.text_start,
            -fragment.page_number,
        ),
    ).page_number


def _metadata(
    row: TableRowData,
    *,
    content_cell: TableCellData,
    part: int,
    total: int,
    source_fragments: list[dict[str, Any]],
) -> dict[str, Any]:
    same_table = [decision.same_table_confidence for decision in row.boundary_decisions]
    continuation = [decision.row_continuation_confidence for decision in row.boundary_decisions]
    return {
        "table_id": row.table_id,
        "row_id": row.row_id,
        "row_index": row.row_index,
        "table_title": row.table_title,
        "section_path": row.section_path,
        "page_start": row.page_start,
        "page_end": row.page_end,
        "table_content_kind": "row",
        "content_column": content_cell.column_name,
        "table_chunk_part": part,
        "table_chunk_total": total,
        "table_text_serialization_version": TABLE_TEXT_SERIALIZATION_VERSION,
        "reconstruction_method": (
            "deterministic_adjacent_pages" if row.boundary_decisions else "deterministic_table_structure"
        ),
        "reconstruction_algorithm_version": row.algorithm_version,
        "same_table_confidence": min(same_table) if same_table else 1.0,
        "row_continuation_confidence": min(continuation) if continuation else 1.0,
        "cell_assignment_confidence": min(
            (cell.assignment_confidence for cell in row.cells),
            default=1.0,
        ),
        "source_fragments": source_fragments,
    }


def chunk_table_row(
    row: TableRowData,
    *,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[TableRowChunk]:
    """Serialize one logical row, splitting its largest cell when necessary."""
    if not row.cells:
        return []
    full_text = _full_row_text(row)
    visible_cells = [
        cell for cell in row.cells if cell.covered_by is None and (cell.text.strip() or cell.explicit_empty)
    ]
    if not visible_cells:
        return []
    fallback_content = max(visible_cells, key=lambda cell: length_function(cell.text))
    identity_columns = set(row.identity_columns)
    content_cells = [
        cell
        for cell in visible_cells
        if cell.column_index not in identity_columns and (cell.text.strip() or cell.explicit_empty)
    ]
    if not content_cells:
        content_cells = [fallback_content]
        identity_columns = {cell.column_index for cell in visible_cells if cell is not fallback_content}
    if length_function(full_text) <= chunk_size:
        primary_content = max(content_cells, key=lambda cell: length_function(cell.text))
        fragments = _dump_fragments(_all_row_fragments(row))
        return [
            TableRowChunk(
                text=full_text,
                page_number=_piece_page_number(
                    primary_content.source_fragments,
                    0,
                    len(primary_content.text),
                    fallback=row.page_start,
                ),
                metadata=_metadata(
                    row,
                    content_cell=primary_content,
                    part=1,
                    total=1,
                    source_fragments=fragments,
                ),
            )
        ]

    initial_content_columns = {cell.column_index for cell in content_cells}
    identity_candidates = _identity_cells(row, initial_content_columns)
    prefix, repeated_identity = _select_repeated_context(
        row,
        identity_candidates,
        chunk_size=chunk_size,
        length_function=length_function,
    )
    repeated_columns = {cell.column_index for cell in repeated_identity}
    content_cells = [
        *content_cells,
        *(cell for cell in identity_candidates if cell.column_index not in repeated_columns),
    ]

    chunks: list[TableRowChunk] = []
    identity_fragments = _dump_fragments(
        [
            *row.scope_fragments,
            *(fragment for cell in repeated_identity for fragment in cell.source_fragments),
        ]
    )
    for content_cell in content_cells:
        heading_template = _select_heading(
            prefix=prefix,
            cell=content_cell,
            chunk_size=chunk_size,
            length_function=length_function,
        )
        content = content_cell.text if content_cell.text.strip() else "No value is present in this column."

        def rendered_length(value: str) -> int:
            return length_function(
                _compose_chunk(
                    prefix,
                    heading_template,
                    normalize_table_text(value),
                )
            )

        fragment_boundaries = {
            boundary
            for fragment in content_cell.source_fragments
            for boundary in (fragment.text_start, fragment.text_end or len(content_cell.text))
            if 0 < boundary < len(content_cell.text)
        }
        pieces = _semantic_split(
            content,
            chunk_size,
            rendered_length,
            hard_boundaries=fragment_boundaries,
        )
        total = len(pieces)
        for part, (piece, start, end) in enumerate(pieces, start=1):
            heading = _part_heading(heading_template, part, total)
            text = _compose_chunk(prefix, heading, normalize_table_text(piece))
            content_fragments = _overlapping_fragments(content_cell.source_fragments, start, end)
            metadata = _metadata(
                row,
                content_cell=content_cell,
                part=part,
                total=total,
                source_fragments=[
                    *identity_fragments,
                    *(fragment.model_dump(mode="json") for fragment in content_fragments),
                ],
            )
            metadata.update(
                {
                    "context_columns": [
                        cell.column_name or f"Column {cell.column_index + 1}" for cell in repeated_identity
                    ],
                    "deferred_context_columns": [
                        cell.column_name or f"Column {cell.column_index + 1}"
                        for cell in identity_candidates
                        if cell.column_index not in repeated_columns
                    ],
                }
            )
            chunks.append(
                TableRowChunk(
                    text=text,
                    page_number=_piece_page_number(
                        content_cell.source_fragments,
                        start,
                        end,
                        fallback=row.page_start,
                    ),
                    metadata=metadata,
                )
            )
    return chunks


def chunk_table_legend(
    legend: TableLegendData,
    *,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[TableRowChunk]:
    """Serialize a table legend independently from the table's data rows."""
    rendered = render_table_legend(legend)
    if not rendered:
        return []

    if length_function(rendered) <= chunk_size:
        groups = [(legend, rendered)]
    else:
        groups: list[tuple[TableLegendData, str]] = []
        for entry in legend.entries:

            def full_render(meaning: str) -> str:
                partial = legend.model_copy(update={"entries": [entry.model_copy(update={"meaning": meaning})]})
                return render_table_legend(partial)

            render_meaning: Callable[[str], str]
            if length_function(full_render("x")) <= chunk_size:
                render_meaning = full_render
            elif length_function(f"{entry.abbreviation} means “x”.") <= chunk_size:

                def render_meaning(meaning: str) -> str:
                    return f"{entry.abbreviation} means “{normalize_table_text(meaning)}”."
            elif length_function(f"{entry.abbreviation}: x") <= chunk_size:

                def render_meaning(meaning: str) -> str:
                    return f"{entry.abbreviation}: {normalize_table_text(meaning)}"
            else:
                render_meaning = normalize_table_text

            pieces = _semantic_split(
                entry.meaning,
                chunk_size,
                lambda meaning: length_function(render_meaning(meaning)),
            )
            groups.extend(
                (
                    legend.model_copy(update={"entries": [entry.model_copy(update={"meaning": piece})]}),
                    render_meaning(piece),
                )
                for piece, _, _ in pieces
            )

    total = len(groups)
    chunks: list[TableRowChunk] = []
    for part, (group, text) in enumerate(groups, start=1):
        fragments = _dump_fragments(
            [
                *group.scope_fragments,
                *(fragment for entry in group.entries for fragment in entry.source_fragments),
            ]
        )
        chunks.append(
            TableRowChunk(
                text=text,
                page_number=legend.page_number,
                metadata={
                    "table_id": legend.table_id,
                    "table_title": legend.table_title,
                    "section_path": legend.section_path,
                    "page_start": legend.page_number,
                    "page_end": legend.page_number,
                    "table_content_kind": "legend",
                    "table_chunk_part": part,
                    "table_chunk_total": total,
                    "table_text_serialization_version": TABLE_TEXT_SERIALIZATION_VERSION,
                    "reconstruction_algorithm_version": legend.algorithm_version,
                    "legend_abbreviations": [entry.abbreviation for entry in group.entries],
                    "source_fragments": fragments,
                },
            )
        )
    return chunks


__all__ = ["TableRowChunk", "chunk_table_legend", "chunk_table_row"]
