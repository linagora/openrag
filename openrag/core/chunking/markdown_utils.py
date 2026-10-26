"""Markdown parsing primitives used by chunking strategies.

Pure functions extracted from ``components/indexer/chunker/utils.py``. They
recognize page markers, image-description blocks, and tables; split a
markdown document into typed elements; and split oversize tables along
their semantic groups.

This module has no IO and no config dependency.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal

from core.utils.exceptions import PipelineError
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

# A figure/table caption: a numbered label opening the line (``*Table 2. …*``,
# ``#### Figure 1.1: …``, ``Tableau 3 - …``). The separator required after the
# number keeps body references (``Table 2 below shows…``, ``see Figure 3,``) out.
_CAPTION_RE = re.compile(
    r"^\W*(?:figure|fig\.|table|tableau|illustration|graphique|sch[ée]ma|exhibit|encadr[ée])"
    r"\s*\d+(?:[.-]\d+)*\W*?[.:–—-](?!\d)",
    re.IGNORECASE,
)
# A credit line (``Source: …``), which travels with an adjacent caption.
_CREDIT_RE = re.compile(r"^\W*source\s*:", re.IGNORECASE)
# A list-of-figures entry ends with its page number (``Figure 2: Map .... 14``).
_PAGE_REFERENCE_RE = re.compile(r"\d+\W*$")


ElementType = Literal["text", "table", "image"]


@dataclass
class MDElement:
    """A typed segment of markdown content with optional source page number."""

    type: ElementType
    content: str
    page_number: int | None = None
    metadata: dict[str, Any] | None = None
    #: The block's caption, also leading ``content`` (see ``attach_captions``).
    caption: str = ""

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


def attach_captions(
    elements: list[MDElement],
    takes_caption: Callable[[MDElement], bool] = lambda block: True,
) -> list[MDElement]:
    """Move each figure/table caption onto the table or image block it labels.

    Parsers put a caption above or below its block, inconsistently even within
    one document, so a caption run (caption paragraphs plus any ``Source:``
    credit) touching a block on one side only belongs to that block. A run alone
    between two blocks follows the document's majority side, defaulting to the
    block above. Only blank lines and page markers may separate a caption from
    its block; a caption with prose on both sides stays where it is. A block
    ``takes_caption`` rejects (a placeholder image) still bounds a run, but a
    caption touching it stays in the text.

    The caption leads the block's content, so it reaches the embedded text, and
    is kept in ``caption`` so a split table can repeat it on every piece.
    """
    parts: list[MDElement] = []
    for element in elements:
        caption = _sole_caption(element)
        if caption:
            element = MDElement(type="text", content=caption)
        if element.type == "text" and parts and parts[-1].type == "text":
            parts[-1] = MDElement(type="text", content=f"{parts[-1].content}\n\n{element.content}")
        else:
            parts.append(element)

    def is_block(i: int) -> bool:
        return 0 <= i < len(parts) and parts[i].type != "text"

    def takes(i: int) -> bool:
        return takes_caption(parts[i])

    claims: list[tuple[int, int, list[int]]] = []  # (text part, block part, caption line indexes)
    contested: list[tuple[int, list[int]]] = []
    for i, part in enumerate(parts):
        if part.type != "text":
            continue
        lines = part.content.split("\n")
        paragraphs = _paragraphs(lines)
        below = _caption_run(lines, paragraphs) if is_block(i - 1) else []
        above = _caption_run(lines, reversed(paragraphs)) if is_block(i + 1) else []
        if below and below == above:
            if takes(i - 1) and takes(i + 1):
                contested.append((i, below))
            continue
        if below and takes(i - 1):
            claims.append((i, i - 1, below))
        if above and takes(i + 1):
            claims.append((i, i + 1, above))

    below_wins = 2 * sum(block < text for text, block, _ in claims) >= len(claims)
    claims += [(text, text - 1 if below_wins else text + 1, run) for text, run in contested]

    captions: dict[int, list[str]] = defaultdict(list)
    moved: dict[int, set[int]] = defaultdict(set)
    for text, block, run in sorted(claims):
        lines = parts[text].content.split("\n")
        # A caption is not a heading, whatever ``#`` the parser gave it.
        captions[block] += [lines[k].strip().lstrip("#").strip() for k in run]
        moved[text].update(run)

    out: list[MDElement] = []
    for i, part in enumerate(parts):
        if i in moved:
            kept = [line for k, line in enumerate(part.content.split("\n")) if k not in moved[i]]
            part = replace(part, content="\n".join(kept).strip())
        if i in captions:
            caption = "\n".join(captions[i])
            part = replace(part, content=f"{caption}\n\n{part.content}", caption=caption)
        if part.content:
            out.append(part)
    return out


def _sole_caption(element: MDElement) -> str | None:
    """The caption a parser emitted as a block of its own, if that is all the block holds.

    Marker renders some captions as a one-cell table padded with empty cells,
    and the captioner transcribes a caption set as a picture. A list of figures
    (one caption per row) holds several captions and is left a table.
    """
    if element.type == "table":
        texts = [cell for row in element.content.splitlines() for cell in row.strip().strip("|").split("|")]
    elif element.type == "image":
        texts = IMAGE_RE.sub(r"\2", element.content).splitlines()
    else:
        return None
    texts = [text.strip() for text in texts if text.strip(" :-")]
    return texts[0] if len(texts) == 1 and _CAPTION_RE.match(texts[0]) else None


def _paragraphs(lines: list[str]) -> list[list[int]]:
    """Line indexes of each paragraph: a run of non-blank lines."""
    paragraphs: list[list[int]] = []
    for k, line in enumerate(lines):
        if not line.strip():
            continue
        if paragraphs and paragraphs[-1][-1] == k - 1:
            paragraphs[-1].append(k)
        else:
            paragraphs.append([k])
    return paragraphs


def _caption_run(lines: list[str], paragraphs: Iterable[list[int]]) -> list[int]:
    """Line indexes of the caption and credit paragraphs at one edge of ``lines``.

    Walks ``paragraphs`` past page markers, stopping at any other paragraph, so
    a caption wrapped over several lines moves whole. Empty unless the run holds
    a caption (a credit alone stays put), and empty for a list of figures:
    several captions that all end with a page number.
    """
    run: list[int] = []
    for paragraph in paragraphs:
        head = lines[paragraph[0]].strip()
        if _CAPTION_RE.match(head) or _CREDIT_RE.match(head):
            run += paragraph
        elif not PAGE_RE.fullmatch(head):
            break
    captions = [lines[k].strip() for k in run if _CAPTION_RE.match(lines[k].strip())]
    if not captions or (len(captions) > 1 and all(_PAGE_REFERENCE_RE.search(c) for c in captions)):
        return []
    return sorted(run)


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

    Each chunk repeats the table's caption and header. When a new chunk starts,
    the LAST row of the previous chunk is replayed as overlap so context is
    preserved across the boundary.
    """
    caption = table_element.caption
    txt = clean_markdown_table_spacing(table_element.content.removeprefix(caption))
    if table_element.metadata and table_element.metadata.get("csv_columns"):
        return _chunk_csv_table(
            txt,
            table_element=table_element,
            chunk_size=chunk_size,
            length_function=length_function,
        )
    header_lines, groups = parse_markdown_table(txt)
    header_text = "\n".join(header_lines)
    if caption:
        header_text = f"{caption}\n\n{header_text}"
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
        return _chunk_csv_row_per_column(
            columns=columns,
            cells=cells,
            row_number=row_number,
            table_element=table_element,
            chunk_size=chunk_size,
            length_function=length_function,
        )

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
            "csv_row_start": row_number,
            "csv_row_end": row_number,
            "csv_row_numbers": [row_number],
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
            raise PipelineError("CSV column group exceeded its token budget")
        split_index = payload_in_group[0]
        split_position = indexes.index(split_index)
        column_name = columns[split_index]
        label = f"[CSV row {row_number}; {column_name} continuation 999/999]"
        reserved_cells = list(group_cells)
        reserved_cells[split_position] = label
        cell_budget = chunk_size - length_function(_markdown_table(group_columns, reserved_cells))
        if cell_budget <= 0:
            return _chunk_csv_row_per_column(
                columns=columns,
                cells=cells,
                row_number=row_number,
                table_element=table_element,
                chunk_size=chunk_size,
                length_function=length_function,
            )
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


def _chunk_csv_row_per_column(
    *,
    columns: list[str],
    cells: list[str],
    row_number: int,
    table_element: MDElement,
    chunk_size: int,
    length_function: Callable[[str], int],
) -> list[MDElement]:
    """Emit one independently bounded group per column of a CSV row."""
    chunks: list[MDElement] = []
    total_groups = len(columns)
    for group_number, (column_name, cell) in enumerate(zip(columns, cells, strict=True), start=1):
        group_columns = [column_name]
        metadata = {
            **(table_element.metadata or {}),
            "csv_columns": group_columns,
            "csv_row_start": row_number,
            "csv_row_end": row_number,
            "csv_row_numbers": [row_number],
            "csv_row_number": row_number,
            "csv_column_group": group_number,
            "csv_column_groups_total": total_groups,
        }
        group_text = _markdown_table(group_columns, [cell])
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

        label = f"[CSV row {row_number}; {column_name} continuation 999/999]"
        cell_budget = chunk_size - length_function(_markdown_table(group_columns, [label]))
        if cell_budget <= 0:
            raise PipelineError(f"CSV column {column_name!r} leaves no room for content within the chunk token budget")
        parts = _split_to_budget(cell, cell_budget, length_function)
        for part_number, part in enumerate(parts, start=1):
            continuation = f"[CSV row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
            chunks.append(
                MDElement(
                    type="table",
                    content=_markdown_table(group_columns, [continuation]),
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
    """Without loss, divide text at whitespace and then characters when necessary."""
    if not text:
        return [""]
    if budget <= 0:
        raise PipelineError("CSV cell split requires a positive token budget")

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
    source_row_numbers = table_element.metadata.get("csv_row_numbers")
    if not isinstance(source_row_numbers, list) or len(source_row_numbers) != len(data_rows):
        source_row_numbers = list(range(first_row_number, first_row_number + len(data_rows)))
    else:
        source_row_numbers = [int(row_number) for row_number in source_row_numbers]
    body_budget = chunk_size - header_tokens
    chunks: list[MDElement] = []
    current_rows: list[str] = []
    current_row_numbers: list[int] = []
    current_start: int | None = None
    current_end: int | None = None

    def emit_current() -> None:
        nonlocal current_rows, current_row_numbers, current_start, current_end
        if not current_rows:
            return
        metadata = {
            **table_element.metadata,
            "csv_row_start": current_start,
            "csv_row_end": current_end,
            "csv_row_numbers": list(current_row_numbers),
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
        current_row_numbers = []
        current_start = None
        current_end = None

    for row_number, row in zip(source_row_numbers, data_rows, strict=True):
        row_tokens = length_function(row)
        if row_tokens <= body_budget:
            if current_rows and length_function("\n".join([*current_rows, row])) > body_budget:
                emit_current()
            current_rows.append(row)
            current_row_numbers.append(row_number)
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
                    metadata={
                        **table_element.metadata,
                        "csv_row_start": row_number,
                        "csv_row_end": row_number,
                        "csv_row_numbers": [row_number],
                    },
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
        cell_budget = body_budget - label_reserve
        if cell_budget <= 0:
            chunks.extend(
                _chunk_csv_row_per_column(
                    columns=columns,
                    cells=cells,
                    row_number=row_number,
                    table_element=table_element,
                    chunk_size=chunk_size,
                    length_function=length_function,
                )
            )
            continue
        parts = _split_to_budget(cells[cell_index], cell_budget, length_function)
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
                        "csv_row_numbers": [row_number],
                        "csv_row_number": row_number,
                        "csv_column": column_name,
                        "csv_part": part_number,
                        "csv_parts_total": len(parts),
                    },
                )
            )

    emit_current()
    return chunks
