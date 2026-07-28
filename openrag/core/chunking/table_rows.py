"""Self-contained chunking for normalized logical table rows."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.models.document import SourceFragment, TableCellData, TableRowData

_SEMANTIC_BOUNDARY_RE = re.compile(r"\n{2,}|(?=\n\s*(?:#{1,6}\s+|[-*•]\s+|\d+(?:[.)]|\.\d+[.)]?)\s+))|(?<=[.!?;:])\s+")


@dataclass(slots=True, frozen=True)
class TableRowChunk:
    text: str
    page_number: int
    metadata: dict[str, Any]


def _identity_cells(row: TableRowData, content_columns: set[int]) -> list[TableCellData]:
    return [cell for cell in row.cells if cell.column_index not in content_columns and cell.text.strip()]


def _context_prefix(row: TableRowData, content_columns: set[int]) -> str:
    lines: list[str] = []
    if row.section_path:
        lines.append(f"Section: {' > '.join(row.section_path)}")
    if row.table_title:
        lines.append(f"Table: {row.table_title}")
    for cell in _identity_cells(row, content_columns):
        lines.append(f"{cell.column_name or f'Column {cell.column_index + 1}'}: {cell.text}")
    return "\n".join(lines)


def _full_row_text(row: TableRowData) -> str:
    lines: list[str] = []
    if row.section_path:
        lines.append(f"Section: {' > '.join(row.section_path)}")
    if row.table_title:
        lines.append(f"Table: {row.table_title}")
    lines.extend(f"{cell.column_name or f'Column {cell.column_index + 1}'}: {cell.text}" for cell in row.cells)
    return "\n".join(lines)


def _truncate_to_budget(text: str, budget: int, length_function: Callable[[str], int]) -> str:
    if budget <= 0:
        return ""
    if length_function(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if length_function(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


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
        "table_title": row.table_title,
        "section_path": row.section_path,
        "page_start": row.page_start,
        "page_end": row.page_end,
        "content_column": content_cell.column_name,
        "table_chunk_part": part,
        "table_chunk_total": total,
        "reconstruction_method": "deterministic_adjacent_pages",
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
    fallback_content = max(row.cells, key=lambda cell: length_function(cell.text))
    identity_columns = set(row.identity_columns)
    content_cells = [cell for cell in row.cells if cell.column_index not in identity_columns and cell.text.strip()]
    if not content_cells:
        content_cells = [fallback_content]
        identity_columns = {cell.column_index for cell in row.cells if cell is not fallback_content}
    if length_function(full_text) <= chunk_size:
        primary_content = max(content_cells, key=lambda cell: length_function(cell.text))
        fragments = [fragment.model_dump(mode="json") for cell in row.cells for fragment in cell.source_fragments]
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

    content_columns = {cell.column_index for cell in content_cells}
    prefix = _context_prefix(row, content_columns)
    prefix_budget = max(1, int(chunk_size * 0.60))
    prefix = _truncate_to_budget(prefix, prefix_budget, length_function)
    chunks: list[TableRowChunk] = []
    identity_fragments = [
        fragment.model_dump(mode="json")
        for cell in _identity_cells(row, content_columns)
        for fragment in cell.source_fragments
    ]
    for content_cell in content_cells:
        label = content_cell.column_name or f"Column {content_cell.column_index + 1}"
        reserve = length_function(f"{prefix}\n{label}, part 999 of 999:")
        content_budget = max(1, chunk_size - reserve)
        fragment_boundaries = {
            boundary
            for fragment in content_cell.source_fragments
            for boundary in (fragment.text_start, fragment.text_end or len(content_cell.text))
            if 0 < boundary < len(content_cell.text)
        }
        pieces = _semantic_split(
            content_cell.text,
            content_budget,
            length_function,
            hard_boundaries=fragment_boundaries,
        )
        total = len(pieces)
        for part, (piece, start, end) in enumerate(pieces, start=1):
            heading = f"{label}, part {part} of {total}:"
            text = f"{prefix}\n{heading}\n{piece}".strip()
            if length_function(text) > chunk_size:
                compact_prefix = _truncate_to_budget(prefix, max(1, chunk_size // 3), length_function)
                text = f"{compact_prefix}\n{heading}\n{piece}".strip()
            content_fragments = _overlapping_fragments(content_cell.source_fragments, start, end)
            chunks.append(
                TableRowChunk(
                    text=text,
                    page_number=_piece_page_number(
                        content_cell.source_fragments,
                        start,
                        end,
                        fallback=row.page_start,
                    ),
                    metadata=_metadata(
                        row,
                        content_cell=content_cell,
                        part=part,
                        total=total,
                        source_fragments=[
                            *identity_fragments,
                            *(fragment.model_dump(mode="json") for fragment in content_fragments),
                        ],
                    ),
                )
            )
    return chunks


__all__ = ["TableRowChunk", "chunk_table_row"]
