"""Markdown parsing primitives used by chunking strategies.

Pure functions extracted from ``components/indexer/chunker/utils.py``. They
recognize page markers, image-description blocks, and tables; split a
markdown document into typed elements; and split oversize tables along
their semantic groups.

This module has no IO and no config dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from core.utils.text import clean_markdown_table_spacing

# Header + delimiter + at least one row. The delimiter row is one or more cells
# of ``:?-+:?`` with optional surrounding spaces/tabs — this matches canonical GFM
# spacing (``| --- | --- |``), alignment colons (``| :--- | ---: |``, ``| :---: |``)
# and the tight form (``|---|---|``) alike. A previous ``\s*[:-]+(?:\s*\|[:-]+)*``
# allowed whitespace only *before* each pipe, so a space *after* a pipe (the
# canonical form) failed to match and the table fell through to plain text (#710).
# In-cell whitespace is ``[ \t]`` (not ``\s``) so a delimiter can't span line
# breaks and swallow following rows as one multi-line "delimiter". No re.DOTALL,
# so a row's ``.*?`` stays on its line (a pipe-looking text line can't be pulled
# into a later table); the final row may end at a newline OR EOF. Each row's
# closing pipe may be followed by ``[ \t]*`` before the line ends — GFM permits
# trailing line whitespace, and without this a stray space after ``|`` would drop
# the whole table back to plain text (the same failure class as #710).
TABLE_RE = re.compile(
    r"((?:^|\n)\|.*?\|[ \t]*\r?\n\|(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*\r?\n(?:\|.*?\|[ \t]*(?:\r?\n|$))+)",
    re.MULTILINE,
)

# `<image_description>...</image_description>` block injected by the VLM step.
IMAGE_RE = re.compile(r"(<image_description>(.*?)</image_description>)", re.DOTALL)

# `[PAGE_N]` page-boundary markers — content BEFORE [PAGE_N] is on page N.
PAGE_RE = re.compile(r"\[PAGE_(\d+)\]")


ElementType = Literal["text", "table", "image"]


@dataclass
class MDElement:
    """A typed segment of markdown content with optional source page number."""

    type: ElementType
    content: str
    page_number: int | None = None
    metadata: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return f"MDElement(type={self.type}, page_number={self.page_number}, content={self.content[:100]}...)"


def span_inside(span: tuple[int, int], container: tuple[int, int]) -> bool:
    """Return True if ``span`` is fully contained within ``container``."""
    return container[0] <= span[0] and span[1] <= container[1]


def get_page_number(position: int, page_markers: list[tuple[int, int]]) -> int:
    """Look up the page number for a position in the source markdown.

    ``page_markers`` is a sorted list of ``(offset, page_n)`` tuples taken
    from ``[PAGE_N]`` matches. Content AFTER ``[PAGE_N]`` belongs to page
    ``N + 1``; content before any marker is page 1.
    """
    current_page = 1
    for marker_pos, page_num in page_markers:
        if position >= marker_pos:
            current_page = page_num + 1
        else:
            break
    return current_page


def split_md_elements(md_text: str) -> list[MDElement]:
    """Split markdown into ``MDElement`` segments of text, table, and image.

    Tables nested inside an ``<image_description>`` block are NOT extracted
    as separate elements — they belong to the image.
    """
    page_markers: list[tuple[int, int]] = []
    for match in PAGE_RE.finditer(md_text):
        page_markers.append((match.start(), int(match.group(1))))
    page_markers.sort()

    all_matches: list[tuple[tuple[int, int], ElementType, str, int | None]] = []
    image_spans: list[tuple[int, int]] = []

    for match in IMAGE_RE.finditer(md_text):
        span = match.span()
        page_num = get_page_number(span[0], page_markers)
        all_matches.append((span, "image", match.group(1).strip(), page_num))
        image_spans.append(span)

    for match in TABLE_RE.finditer(md_text):
        span = match.span()
        if not any(span_inside(span, image_span) for image_span in image_spans):
            page_num = get_page_number(span[0], page_markers)
            all_matches.append((span, "table", match.group(1).strip(), page_num))

    all_matches.sort(key=lambda x: x[0][0])

    parts: list[MDElement] = []
    last = 0

    for (start, end), match_type, content, page_num in all_matches:
        if start > last:
            text_segment = md_text[last:start]
            if text_segment.strip():
                parts.append(MDElement(type="text", content=text_segment.strip()))
        parts.append(MDElement(type=match_type, content=content, page_number=page_num))
        last = end

    if last < len(md_text):
        remaining = md_text[last:]
        if remaining.strip():
            parts.append(MDElement(type="text", content=remaining.strip()))

    return parts


def get_chunk_page_number(chunk_str: str, previous_chunk_ending_page: int = 1) -> dict[str, int]:
    """Resolve start and end pages for a text chunk containing ``[PAGE_N]`` markers.

    Returns ``{"start_page": int, "end_page": int}``.
    """
    matches = list(PAGE_RE.finditer(chunk_str))

    if not matches:
        return {
            "start_page": previous_chunk_ending_page,
            "end_page": previous_chunk_ending_page,
        }

    first_match = matches[0]
    last_match = matches[-1]
    last_char_idx = len(chunk_str) - 1

    if first_match.start() == 0:
        start_page = int(first_match.group(1)) + 1
    else:
        start_page = previous_chunk_ending_page

    if last_match.end() - 1 == last_char_idx:
        end_page = int(last_match.group(1))
    else:
        end_page = int(last_match.group(1)) + 1

    return {"start_page": start_page, "end_page": max(start_page, end_page)}


def parse_markdown_table(markdown_table: str) -> tuple[list[str], list[list[str]]]:
    """Parse a markdown table into header lines + groups of rows.

    Rows are grouped by the first column ("Domain"): a non-empty Domain
    starts a new group, an empty Domain continues the current group. This
    preserves the document's logical structure when chunking large tables.
    """
    lines = markdown_table.strip().split("\n")
    header_lines = lines[:2]
    data_rows = lines[2:]

    groups: list[list[str]] = []
    current_group: list[str] = []

    for row in data_rows:
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        if not cells:
            continue
        domain = cells[0]
        if domain:
            if current_group:
                groups.append(current_group)
            current_group = [row]
        else:
            current_group.append(row)

    if current_group:
        groups.append(current_group)

    return header_lines, groups


def chunk_table(
    table_element: MDElement,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[MDElement]:
    """Split an oversize markdown table into multiple ``MDElement`` chunks.

    Each chunk repeats the table header. When a new chunk starts, the LAST
    row of the previous chunk is replayed as overlap so context is preserved
    across the boundary.
    """
    txt = clean_markdown_table_spacing(table_element.content)
    if table_element.metadata and table_element.metadata.get("csv_columns"):
        return _chunk_csv_table(
            txt,
            table_element=table_element,
            chunk_size=chunk_size,
            length_function=length_function,
        )
    header_lines, groups = parse_markdown_table(txt)
    header_text = "\n".join(header_lines)
    group_texts = ["\n".join(g) for g in groups]
    header_ntoks = length_function(header_text)
    groups_ntoks = [length_function(g) for g in group_texts]
    subtables: list[str] = []
    body_rows: list[str] = []  # rows under the current chunk, header excluded
    body_size = 0
    prev_last_row: str | None = None

    for group_txt, g_ntoks in zip(group_texts, groups_ntoks, strict=True):
        # Only flush when we actually have body content to flush — otherwise an
        # oversized first group would emit a header-only chunk.
        if body_rows and header_ntoks + body_size + g_ntoks > chunk_size:
            subtables.append("\n".join([header_text, *body_rows]))
            body_rows = []
            body_size = 0
            # Replay only the last row of the previous chunk as overlap
            # (matches the docstring contract : prev_last_row is the trailing
            # line of the last admitted group).
            if prev_last_row:
                body_rows.append(prev_last_row)
                body_size += length_function(prev_last_row)
        body_rows.append(group_txt)
        body_size += g_ntoks
        # The "last row" is the trailing line of this group, not the whole group.
        prev_last_row = group_txt.rsplit("\n", 1)[-1]

    if body_rows:
        subtables.append("\n".join([header_text, *body_rows]))

    return [MDElement(type="table", content=subtable, page_number=table_element.page_number) for subtable in subtables]


def _table_cells(row: str) -> list[str]:
    """Return cells from a canonical Markdown row that was emitted by ``CsvParser``.

    CSV pipe characters are rendered as ``&#124;`` before this point, so a
    simple split is safe here. This intentionally does not try to parse every
    variant of hand-written Markdown. The CSV path only receives canonical rows.
    """
    return [cell.strip() for cell in row.strip().split("|")[1:-1]]


def _markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _markdown_table(columns: list[str], cells: list[str]) -> str:
    """Render one canonical CSV row with its own Markdown header."""
    return "\n".join(
        [
            _markdown_row(columns),
            _markdown_row(["---"] * len(columns)),
            _markdown_row(cells),
        ]
    )


def _identity_column_indexes(columns: list[str]) -> list[int]:
    """Choose compact, generic row identifiers for a CSV column group.

    Prefer a conventional identifier and then one human-readable label.  The
    first column is the stable fallback for files without either convention.
    These values repeat in each group so retrieval can still associate a group
    with the original row without repeating every wide-table field.
    """
    normalized = [re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_") for column in columns]
    identifiers = [
        index
        for index, name in enumerate(normalized)
        if name in {"id", "uuid", "identifier", "key"} or name.endswith("_id")
    ]
    labels = [
        index
        for index, name in enumerate(normalized)
        if name in {"name", "title", "label", "legal_name", "preferred_name"} or name.endswith("_name")
    ]

    selected: list[int] = []
    if identifiers:
        selected.append(identifiers[0])
    if labels:
        selected.append(next((index for index in labels if index not in selected), labels[0]))
    if not selected and columns:
        selected.append(0)
    return selected


def _requires_csv_column_groups(
    columns: list[str],
    cells: list[str],
    *,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> bool:
    """True when width, rather than one large cell, overflowed the row.

    Removing the largest value isolates the width of the remaining schema and
    scalar values.  A narrow row with one large note keeps the established
    continuation format; a row whose remaining fields still overflow uses
    column groups.
    """
    if len(columns) < 2:
        return False
    largest_index = max(range(len(cells)), key=lambda index: length_function(cells[index]))
    remaining = [index for index in range(len(columns)) if index != largest_index]
    return (
        length_function(_markdown_table([columns[index] for index in remaining], [cells[index] for index in remaining]))
        > chunk_size
    )


def _chunk_wide_csv_row(
    *,
    columns: list[str],
    cells: list[str],
    row_number: int,
    table_element: MDElement,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[MDElement]:
    """Split one wide CSV row into token-bounded vertical column groups.

    Each group keeps stable identifiers and a subset of the original columns.
    Values are never omitted: identity values repeat, and every other value
    occurs in exactly one group.  A cell too large for its own group reuses the
    existing lossless continuation split, now with only that group's columns.
    """
    identity_indexes = _identity_column_indexes(columns)
    payload_indexes = [index for index in range(len(columns)) if index not in identity_indexes]
    if not payload_indexes:
        return []

    group_indexes: list[list[int]] = []
    current_payload: list[int] = []
    for payload_index in payload_indexes:
        candidate = [*current_payload, payload_index]
        candidate_indexes = [*identity_indexes, *candidate]
        candidate_text = _markdown_table(
            [columns[index] for index in candidate_indexes],
            [cells[index] for index in candidate_indexes],
        )
        if current_payload and length_function(candidate_text) > chunk_size:
            group_indexes.append([*identity_indexes, *current_payload])
            current_payload = [payload_index]
        else:
            current_payload = candidate
    if current_payload:
        group_indexes.append([*identity_indexes, *current_payload])

    total_groups = len(group_indexes)
    chunks: list[MDElement] = []
    for group_number, indexes in enumerate(group_indexes, start=1):
        group_columns = [columns[index] for index in indexes]
        group_cells = [cells[index] for index in indexes]
        group_text = _markdown_table(group_columns, group_cells)
        metadata = {
            **(table_element.metadata or {}),
            "csv_columns": group_columns,
            "csv_source_columns": columns,
            "csv_row_start": row_number,
            "csv_row_end": row_number,
            "csv_row_number": row_number,
            "csv_column_group": group_number,
            "csv_column_groups_total": total_groups,
        }
        if length_function(group_text) <= chunk_size:
            chunks.append(
                MDElement(
                    type="table",
                    content=group_text,
                    page_number=table_element.page_number,
                    metadata=metadata,
                )
            )
            continue

        # Greedy grouping leaves an oversize group only when its one payload
        # cell exceeds the budget alongside the identity columns.
        payload_in_group = [index for index in indexes if index not in identity_indexes]
        if len(payload_in_group) != 1:
            raise ValueError("CSV column group exceeded its token budget")
        split_index = payload_in_group[0]
        split_position = indexes.index(split_index)
        column_name = columns[split_index]
        label = f"[CSV row {row_number}; {column_name} continuation 999/999]"
        reserved_cells = list(group_cells)
        reserved_cells[split_position] = label
        cell_budget = max(1, chunk_size - length_function(_markdown_table(group_columns, reserved_cells)))
        parts = _split_to_budget(cells[split_index], cell_budget, length_function)
        for part_number, part in enumerate(parts, start=1):
            continuation_cells = list(group_cells)
            continuation_cells[split_position] = (
                f"[CSV row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
            )
            chunks.append(
                MDElement(
                    type="table",
                    content=_markdown_table(group_columns, continuation_cells),
                    page_number=table_element.page_number,
                    metadata={
                        **metadata,
                        "csv_column": column_name,
                        "csv_part": part_number,
                        "csv_parts_total": len(parts),
                    },
                )
            )
    return chunks


def _split_to_budget(text: str, budget: int, length_function: Callable[[str], int]) -> list[str]:
    """Without loss divide text at whitespace and then characters if it isnecessary."""
    if not text:
        return [""]
    if budget <= 0:
        return [text]

    pieces: list[str] = []
    remaining = text
    while remaining and length_function(remaining) > budget:
        # prefer the largest whitespace boundary rather than one over the token budget
        boundaries = [match.end() for match in re.finditer(r"\s+", remaining)]
        cut = None
        low, high = 0, len(boundaries) - 1
        while low <= high:
            middle = (low + high) // 2
            boundary = boundaries[middle]
            if length_function(remaining[:boundary]) <= budget:
                cut = boundary
                low = middle + 1
            else:
                high = middle - 1
        if cut is None:
            #  single long word or a URL can still exceed the budget
            # so find the largest character prefix that fits without discarding anything.
            low, high = 1, len(remaining)
            best = 0
            while low <= high:
                middle = (low + high) // 2
                if length_function(remaining[:middle]) <= budget:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            cut = best or 1
        # Keep the separator at the beginning of the following part because stripping
        # it would silently change the original CSV cell when all continuation
        # parts are joined again.
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not pieces:
        pieces.append(remaining)
    return pieces


def _chunk_csv_table(
    table: str,
    *,
    table_element: MDElement,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[MDElement]:
    """Pack complete CSV rows and split oversized records when required.

    Every emitted piece repeats the two Markdown header rows. Normal CSV rows
    are never split or overlapped. A width-driven overflow becomes vertical
    column groups; a narrow row with one large cell keeps labelled continuation
    rows.
    """
    lines = table.strip().split("\n")
    if len(lines) <= 2:
        return [
            MDElement(
                type="table", content=table, page_number=table_element.page_number, metadata=table_element.metadata
            )
        ]

    header_lines = lines[:2]
    data_rows = lines[2:]
    header_text = "\n".join(header_lines)
    header_tokens = length_function(header_text)
    columns = list(table_element.metadata.get("csv_columns") or _table_cells(header_lines[0]))
    first_row_number = int(table_element.metadata.get("csv_row_start", 2))
    body_budget = max(1, chunk_size - header_tokens)
    chunks: list[MDElement] = []
    current_rows: list[str] = []
    current_start: int | None = None
    current_end: int | None = None

    def emit_current() -> None:
        nonlocal current_rows, current_start, current_end
        if not current_rows:
            return
        metadata = {
            **table_element.metadata,
            "csv_row_start": current_start,
            "csv_row_end": current_end,
        }
        chunks.append(
            MDElement(
                type="table",
                content="\n".join([header_text, *current_rows]),
                page_number=table_element.page_number,
                metadata=metadata,
            )
        )
        current_rows = []
        current_start = None
        current_end = None

    for offset, row in enumerate(data_rows):
        row_number = first_row_number + offset
        row_tokens = length_function(row)
        if row_tokens <= body_budget:
            if current_rows and length_function("\n".join([*current_rows, row])) > body_budget:
                emit_current()
            current_rows.append(row)
            current_start = row_number if current_start is None else current_start
            current_end = row_number
            continue

        emit_current()
        cells = _table_cells(row)
        if len(cells) != len(columns):
            # canonical CSV output always has matching cell counts
            # keeping anunexpected row whole is safer than silently corrupting it
            chunks.append(
                MDElement(
                    type="table",
                    content="\n".join([header_text, row]),
                    page_number=table_element.page_number,
                    metadata={**table_element.metadata, "csv_row_start": row_number, "csv_row_end": row_number},
                )
            )
            continue

        if _requires_csv_column_groups(
            columns,
            cells,
            chunk_size=chunk_size,
            length_function=length_function,
        ):
            grouped_chunks = _chunk_wide_csv_row(
                columns=columns,
                cells=cells,
                row_number=row_number,
                table_element=table_element,
                chunk_size=chunk_size,
                length_function=length_function,
            )
            if grouped_chunks:
                chunks.extend(grouped_chunks)
                continue

        # split the largest cell (the label keeps its column name and part
        # number visible to the LLM) + unchanged cells retain the whole row identity
        cell_index = max(range(len(cells)), key=lambda index: length_function(cells[index]))
        column_name = columns[cell_index]
        base_cells = list(cells)
        base_cells[cell_index] = f"[CSV row {row_number}; {column_name} continuation 999/999]"
        label_reserve = length_function(_markdown_row(base_cells))
        parts = _split_to_budget(cells[cell_index], max(1, body_budget - label_reserve), length_function)
        for part_number, part in enumerate(parts, start=1):
            continuation_cells = list(cells)
            continuation_cells[cell_index] = (
                f"[CSV row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
            )
            continuation_row = _markdown_row(continuation_cells)
            chunks.append(
                MDElement(
                    type="table",
                    content="\n".join([header_text, continuation_row]),
                    page_number=table_element.page_number,
                    metadata={
                        **table_element.metadata,
                        "csv_row_start": row_number,
                        "csv_row_end": row_number,
                        "csv_row_number": row_number,
                        "csv_column": column_name,
                        "csv_part": part_number,
                        "csv_parts_total": len(parts),
                    },
                )
            )

    emit_current()
    return chunks
