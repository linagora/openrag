"""Markdown parsing primitives used by chunking strategies.

Pure functions extracted from ``components/indexer/chunker/utils.py``. They
recognize page markers, image-description blocks, and tables; split a
markdown document into typed elements; and split oversize tables along
their semantic groups.

This module has no IO and no config dependency.
"""

from __future__ import annotations

import bisect
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from operator import itemgetter
from typing import Any, Literal

from core.utils.consts import XLSX_TEXT_NUMBER_FORMAT_KINDS
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
# N is read with int(), which refuses more than 4,300 digits: the marker can
# also come from the document's own text, so a number no page count reaches
# leaves it plain text.
PAGE_RE = re.compile(r"\[PAGE_(\d{1,9})\]")

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
# Four digits are a year (``Market share in 2022``), not a page.
_PAGE_REFERENCE_RE = re.compile(r"(?<!\d)\d{1,3}\W*$")
# A Markdown heading opens a new section, so it never continues a caption.
_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


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
    i = bisect.bisect_right(page_markers, position, key=itemgetter(0))
    return page_markers[i - 1][1] + 1 if i else 1


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
            if takes_caption(parts[i - 1]) and takes_caption(parts[i + 1]):
                contested.append((i, below))
            continue
        if below and takes_caption(parts[i - 1]):
            claims.append((i, i - 1, below))
        if above and takes_caption(parts[i + 1]):
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
    """Line indexes of each paragraph: a run of non-blank lines, which a heading always opens."""
    paragraphs: list[list[int]] = []
    for k, line in enumerate(lines):
        if not line.strip():
            continue
        if paragraphs and paragraphs[-1][-1] == k - 1 and not _MD_HEADING_RE.match(line):
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
    if table_element.metadata and table_element.metadata.get("tabular_format") == "xlsx":
        return _chunk_xlsx_table(table_element.content, table_element, chunk_size, length_function)
    txt = clean_markdown_table_spacing(table_element.content)
    if table_element.metadata and table_element.metadata.get("csv_columns"):
        if table_element.metadata.get("tabular_format") == "xlsx":
            return _chunk_xlsx_table(txt, table_element, chunk_size, length_function)
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


# A worksheet context (notes above a table) is repeated on every table chunk.
# Past this share of the chunk budget it costs more row space than it is worth,
# so it is indexed once, as its own chunks, with the table's first batch.
_XLSX_SHEET_CONTEXT_MAX_SHARE = 0.25
_XLSX_SHEET_CONTEXT_HEADING = "Worksheet context (before column headers):"
# Formula and display context is optional text: at most this share of a chunk.
_XLSX_SUPPLEMENTAL_CONTEXT_MAX_SHARE = 0.2
# Metadata that describes table rows/columns and must not travel to context chunks.
_XLSX_TABLE_ONLY_PREFIXES = ("csv_", "formula_", "number_format_", "table_header", "table_row", "table_column")


def _xlsx_sheet_context_chunks(sheet_context, element, chunk_size, length_function):
    """Split a worksheet context into text chunks that carry the worksheet name."""
    worksheet_context = element.metadata.get("worksheet_context", "")
    lines = [line for line in sheet_context.strip().split("\n")[1:] if line.strip()]

    def render(parts):
        return _XLSX_SHEET_CONTEXT_HEADING + "\n" + "\n".join(parts) + worksheet_context

    budget = chunk_size - length_function(render([])) - 2 * length_function("\n")
    if budget <= 0:
        raise PipelineError("XLSX worksheet name exceeds chunk token budget; increase chunk size")
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        for part in _split_to_budget(line, budget, length_function) if length_function(line) > budget else [line]:
            if current and length_function(render([*current, part])) > chunk_size:
                groups.append(current)
                current = []
            current.append(part)
    if current:
        groups.append(current)
    metadata = {key: value for key, value in element.metadata.items() if not key.startswith(_XLSX_TABLE_ONLY_PREFIXES)}
    # The batch's data-row citation does not describe the context rows.
    metadata.pop("sheet_context", None)
    metadata.pop("cell_range", None)
    metadata["xlsx_content_kind"] = "sheet_context"
    return [
        MDElement(type="text", content=render(group), page_number=element.page_number, metadata=dict(metadata))
        for group in groups
    ]


def _chunk_xlsx_table(table, element, chunk_size, length_function):
    """Reuse row splitting with escaped headers and exact sheet/column metadata."""
    sheet_context = element.metadata.get("sheet_context", "")
    base_context = element.metadata.get("worksheet_context", "") + sheet_context
    formula_context = element.metadata.get("formula_context", "")
    number_format_context = element.metadata.get("number_format_context", "")
    include_number_format_context = bool(number_format_context)
    complete_context = number_format_context + base_context + formula_context
    if complete_context:
        # Chunkers strip block text, which removes trailing whitespace from
        # whichever context comes last.
        stripped_context = complete_context.rstrip()
        if table.endswith(complete_context):
            table = table[: -len(complete_context)]
        elif stripped_context and table.endswith(stripped_context):
            table = table[: -len(stripped_context)]
        else:
            raise PipelineError("XLSX sheet context does not match table content")
    context_chunks: list[MDElement] = []
    worksheet_context = element.metadata.get("worksheet_context", "")
    rows = element.metadata.get("sheet_context_row_numbers") or []
    span = f"rows {rows[0]}-{rows[-1]}" if len(rows) > 1 else f"row {rows[0]}" if rows else "above the table"
    pointer = f"\n\nWorksheet context ({span}) is indexed in separate chunks."
    context_tokens = length_function(sheet_context) if sheet_context else 0
    detach_sheet_context = (
        context_tokens > chunk_size * _XLSX_SHEET_CONTEXT_MAX_SHARE
        # A note no longer than about two pointers is not worth detaching.
        and context_tokens > 2 * length_function(pointer)
        and length_function(table + complete_context) > chunk_size
    )
    if length_function(worksheet_context + pointer) >= chunk_size:
        pointer = ""
    if detach_sheet_context:
        base_context = worksheet_context + pointer
        if element.metadata.get("table_batch_index", 1) == 1:
            context_chunks = _xlsx_sheet_context_chunks(sheet_context, element, chunk_size, length_function)
    table = clean_markdown_table_spacing(table)
    base_context_tokens = length_function(base_context) if base_context else 0
    if base_context_tokens >= chunk_size:
        raise PipelineError("XLSX sheet context exceeds chunk token budget; increase chunk size")
    separator_tokens = length_function("\n")
    original_columns = list(element.metadata["table_columns"])
    rendered_columns = _table_cells(table.split("\n", 1)[0])
    if len(rendered_columns) != len(original_columns):
        raise PipelineError("XLSX table header does not match its column metadata")
    column_positions = {name: index for index, name in enumerate(rendered_columns)}
    if len(column_positions) != len(rendered_columns):
        raise PipelineError("XLSX rendered column names are ambiguous")
    safe_element = MDElement(
        type="table",
        content=table,
        page_number=element.page_number,
        metadata={**element.metadata, "table_columns": rendered_columns, "csv_columns": rendered_columns},
    )
    # Rows first. Formula and display context is supplemental: reserve a fixed
    # share of the budget for it (less when it is smaller), split the rows once,
    # then add context lines to each chunk while they fit. Everything else stays
    # in metadata, so no table explodes into per-column chunks or is rejected
    # because of optional text.
    text_number_format_groups = [
        group
        for group in element.metadata.get("number_format_groups", [])
        if include_number_format_context and group.get("kind") in XLSX_TEXT_NUMBER_FORMAT_KINDS
    ]
    full_supplemental = _xlsx_number_format_context(text_number_format_groups) + _xlsx_formula_context(
        element.metadata.get("formula_groups", [])
    )
    supplemental_budget = (
        min(int(chunk_size * _XLSX_SUPPLEMENTAL_CONTEXT_MAX_SHARE), length_function(full_supplemental))
        if full_supplemental
        else 0
    )
    if separator_tokens + base_context_tokens + supplemental_budget >= chunk_size:
        supplemental_budget = 0
    # Reserve the separator between header and body (the shared CSV splitter
    # counts those independently). Verify the complete rendered result below.
    chunks = _chunk_csv_table(
        table,
        table_element=safe_element,
        chunk_size=chunk_size - separator_tokens - base_context_tokens - supplemental_budget,
        length_function=length_function,
    )
    for chunk in chunks:
        metadata = chunk.metadata
        if detach_sheet_context:
            # The full context lives in its own chunks, not on every row chunk.
            metadata.pop("sheet_context", None)
            metadata["sheet_context_indexed_separately"] = True
        positions = [column_positions[name] for name in metadata["table_columns"]]
        metadata["table_column_indices"] = [index + 1 for index in positions]
        metadata["table_columns"] = [original_columns[index] for index in positions]
        metadata["csv_columns"] = list(metadata["table_columns"])
        for key in ("column_group", "column_groups_total", "part", "parts_total"):
            if f"csv_{key}" in metadata:
                metadata[f"table_{key}"] = metadata[f"csv_{key}"]
        if "csv_column" in metadata:
            name = original_columns[column_positions[metadata["csv_column"]]]
            metadata.update(table_column=name, csv_column=name)

        def column_name(index):
            name = ""
            while index:
                index, remainder = divmod(index - 1, 26)
                name = chr(65 + remainder) + name
            return name

        sheet = "'" + metadata["sheet_name"].replace("'", "''") + "'!"
        start, end = metadata["table_row_start"], metadata["table_row_end"]
        worksheet_columns = metadata.get("table_worksheet_column_indices")
        selected_worksheet_columns = (
            [worksheet_columns[index] for index in positions]
            if isinstance(worksheet_columns, list) and len(worksheet_columns) == len(original_columns)
            else [metadata.get("table_start_column", 1) + index for index in positions]
        )
        selected_excel_columns = {column_name(index) for index in selected_worksheet_columns}
        selected_groups = [
            group
            for group in metadata.get("formula_groups", [])
            if group.get("target_column") in selected_excel_columns
        ]
        selected_cells = [
            cell
            for cell in metadata.get("formula_cells", [])
            if isinstance(cell, dict)
            and (
                _formula_truncation_marker_matches(
                    cell,
                    metadata["table_row_numbers"],
                    selected_excel_columns,
                )
                if cell.get("truncated")
                else cell.get("target_column") in selected_excel_columns
            )
        ]
        selected_number_formats = [
            group
            for group in metadata.get("number_format_groups", [])
            if group.get("target_column") in selected_excel_columns
        ]
        if selected_groups:
            metadata["formula_groups"] = selected_groups
        else:
            metadata.pop("formula_groups", None)
        if selected_cells:
            metadata["formula_cells"] = selected_cells
        else:
            metadata.pop("formula_cells", None)
        if selected_number_formats:
            metadata["number_format_groups"] = selected_number_formats
        else:
            metadata.pop("number_format_groups", None)
        # Formulas first, then percentage and currency displays, while they fit.
        text_formulas: list[dict] = []
        text_number_formats: list[dict] = []
        candidates = [(text_formulas, group) for group in selected_groups] + [
            (text_number_formats, group)
            for group in selected_number_formats
            if include_number_format_context and group.get("kind") in XLSX_TEXT_NUMBER_FORMAT_KINDS
        ]
        context_truncated = False
        for target, group in candidates:
            target.append(group)
            rendered = _xlsx_number_format_context(text_number_formats) + _xlsx_formula_context(text_formulas)
            if length_function(rendered) > supplemental_budget:
                target.pop()
                context_truncated = True
        if context_truncated:
            metadata["supplemental_context_truncated"] = True
        else:
            metadata.pop("supplemental_context_truncated", None)
        chunk_formula_context = _xlsx_formula_context(text_formulas)
        if chunk_formula_context:
            metadata["formula_context"] = chunk_formula_context
        else:
            metadata.pop("formula_context", None)
        chunk_number_format_context = _xlsx_number_format_context(text_number_formats)
        if chunk_number_format_context:
            metadata["number_format_context"] = chunk_number_format_context
        else:
            metadata.pop("number_format_context", None)
        chunk_context = chunk_number_format_context + base_context + chunk_formula_context
        chunk.content += chunk_context
        chunk_context_tokens = length_function(chunk_context) if chunk_context else 0
        csv_effective_chunk_size = metadata.get("csv_effective_chunk_size")
        allowed_size = (
            csv_effective_chunk_size + length_function("\n") + chunk_context_tokens
            if csv_effective_chunk_size is not None
            else chunk_size
        )
        if length_function(chunk.content) > allowed_size:
            raise PipelineError("XLSX chunk exceeds its token budget")
        metadata["cell_range"] = (
            f"{sheet}{column_name(min(selected_worksheet_columns))}{start}:"
            f"{column_name(max(selected_worksheet_columns))}{end}"
        )
        # The bounding range can span blank rows/unselected columns. Exact
        # membership is table_row_numbers plus table_column_indices.
    return [*context_chunks, *chunks]


def _formula_truncation_marker_matches(marker, row_numbers, selected_columns=None):
    """Return whether a truncated-formula marker concerns a chunk's cells.

    Rows are paired with their target column: a chunk matches only when it
    holds one of a column's omitted rows *and* that column. The "*" entry pools
    columns past the marker's bound and matches any selected column.
    """
    if not marker.get("truncated"):
        return False
    for entry in marker.get("omitted_cells_by_column", []):
        if not isinstance(entry, dict):
            continue
        column = entry.get("column")
        if selected_columns is not None and column != "*" and column not in selected_columns:
            continue
        if any(
            row_range.get("start_row", 0) <= row <= row_range.get("end_row", -1)
            for row_range in entry.get("row_ranges", [])
            if isinstance(row_range, dict)
            for row in row_numbers
        ):
            return True
    return False


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


def _tabular_row_metadata(
    metadata: dict,
    row_numbers: list[int],
    *,
    columns: list[str] | None = None,
) -> dict:
    """Update both generic table metadata and transitional CSV aliases."""
    updated = {
        **metadata,
        "csv_row_start": row_numbers[0],
        "csv_row_end": row_numbers[-1],
        "csv_row_numbers": list(row_numbers),
    }
    if columns is not None:
        updated["csv_columns"] = list(columns)
    if metadata.get("tabular_format"):
        updated.update(
            {
                "table_row_start": row_numbers[0],
                "table_row_end": row_numbers[-1],
                "table_row_numbers": list(row_numbers),
            }
        )
        if columns is not None:
            updated["table_columns"] = list(columns)
    if len(row_numbers) == 1:
        updated["csv_row_number"] = row_numbers[0]
        if metadata.get("tabular_format"):
            updated["table_row_number"] = row_numbers[0]
    row_header_paths = metadata.get("table_row_header_paths")
    if isinstance(row_header_paths, dict):
        updated["table_row_header_paths"] = {
            str(row_number): row_header_paths[str(row_number)]
            for row_number in row_numbers
            if str(row_number) in row_header_paths
        }
    formula_groups = metadata.get("formula_groups")
    if isinstance(formula_groups, list):
        selected_rows = set(row_numbers)
        selected_groups = [
            group
            for group in formula_groups
            if isinstance(group, dict)
            and any(group.get("start_row", 0) <= row <= group.get("end_row", -1) for row in selected_rows)
        ]
        if selected_groups:
            updated["formula_groups"] = selected_groups
        else:
            updated.pop("formula_groups", None)
    formula_cells = metadata.get("formula_cells")
    if isinstance(formula_cells, list):
        selected_cells = [
            cell
            for cell in formula_cells
            if isinstance(cell, dict)
            and (
                _formula_truncation_marker_matches(cell, row_numbers)
                if cell.get("truncated")
                else cell.get("row") in set(row_numbers)
            )
        ]
        if selected_cells:
            updated["formula_cells"] = selected_cells
        else:
            updated.pop("formula_cells", None)
    number_format_groups = metadata.get("number_format_groups")
    if isinstance(number_format_groups, list):
        selected_rows = set(row_numbers)

        def intersects(group: dict) -> bool:
            ranges = group.get("row_ranges")
            if isinstance(ranges, list) and not group.get("row_ranges_truncated"):
                return any(
                    isinstance(item, dict)
                    and any(item.get("start_row", 0) <= row <= item.get("end_row", -1) for row in selected_rows)
                    for item in ranges
                )
            return any(group.get("start_row", 0) <= row <= group.get("end_row", -1) for row in selected_rows)

        selected_number_formats = [
            group for group in number_format_groups if isinstance(group, dict) and intersects(group)
        ]
        if selected_number_formats:
            updated["number_format_groups"] = selected_number_formats
        else:
            updated.pop("number_format_groups", None)
    updated.pop("formula_context", None)
    updated.pop("number_format_context", None)
    return updated


def _xlsx_formula_context(groups: list[dict]) -> str:
    if not groups:
        return ""

    def single_line(value) -> str:
        return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

    lines = ["Formula context (calculated columns):"]
    for group in groups:
        lines.append(
            f"- {group['target_column']} ({single_line(group['target_header'])}), {group['range']}: "
            f"{single_line(group['semantic_expression'])} [pattern: {single_line(group['pattern'])}]"
        )
    return "\n\n" + "\n".join(lines)


def _xlsx_number_format_context(groups: list[dict]) -> str:
    if not groups:
        return ""

    def single_line(value) -> str:
        return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

    lines = ["Display format context (raw values remain indexed):"]
    for group in groups:
        location = (
            group["range"]
            if len(group.get("row_ranges", [])) == 1
            else f"{group['cell_count']} cells in {group['range']}"
        )
        lines.append(
            f"- {group['target_column']} ({single_line(group['target_header'])}), {location}: "
            f"{group['kind']} format {single_line(group['format_code'])}; example raw "
            f"{single_line(group['example_raw'])} -> display {single_line(group['display_example'])}"
        )
    return "\n\n" + "\n".join(lines)


def _tabular_label(table_element: MDElement) -> str:
    return str((table_element.metadata or {}).get("tabular_format") or "CSV").upper()


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
            **_tabular_row_metadata(table_element.metadata or {}, [row_number], columns=group_columns),
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
        source_label = _tabular_label(table_element)
        label = f"[{source_label} row {row_number}; {column_name} continuation 999/999]"
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
                f"[{source_label} row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
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
            **_tabular_row_metadata(table_element.metadata or {}, [row_number], columns=group_columns),
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

        source_label = _tabular_label(table_element)
        label = f"[{source_label} row {row_number}; {column_name} continuation 999/999]"
        reserved_tokens = length_function(_markdown_table(group_columns, [label]))
        # A header is indivisible: when it alone exhausts the configured budget,
        # use the smallest viable budget instead of rejecting the whole file.
        # The expanded budget is exposed in metadata so this degradation is
        # observable without dropping or truncating the original column name.
        effective_chunk_size = max(chunk_size, reserved_tokens + 1)
        cell_budget = effective_chunk_size - reserved_tokens
        if effective_chunk_size > chunk_size:
            metadata["csv_effective_chunk_size"] = effective_chunk_size
        parts = _split_to_budget(cell, cell_budget, length_function)
        for part_number, part in enumerate(parts, start=1):
            continuation = (
                f"[{source_label} row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
            )
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


def _largest_fitting_prefix_end(
    text: str,
    start: int,
    budget: int,
    length_function: Callable[[str], int],
) -> int:
    """Find a bounded prefix by probing forward instead of rescanning the suffix."""
    text_length = len(text)
    first_end = start + 1
    if length_function(text[start:first_end]) > budget:
        # Preserve the established progress guarantee even for a length function
        # where one character is already larger than the available budget.
        return first_end

    best = first_end
    span = 2
    first_over_budget = text_length
    while best < text_length:
        probe = min(text_length, start + span)
        if length_function(text[start:probe]) <= budget:
            best = probe
            if probe == text_length:
                return best
            span *= 2
            continue
        first_over_budget = probe
        break

    low, high = best + 1, first_over_budget - 1
    while low <= high:
        middle = (low + high) // 2
        if length_function(text[start:middle]) <= budget:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _split_to_budget(text: str, budget: int, length_function: Callable[[str], int]) -> list[str]:
    """Without loss, divide text at whitespace and then characters when necessary."""
    if not text:
        return [""]
    if budget <= 0:
        raise PipelineError("CSV cell split requires a positive token budget")

    pieces: list[str] = []
    boundaries = [match.end() for match in re.finditer(r"\s+", text)]
    start = 0
    text_length = len(text)
    while start < text_length:
        fitting_end = _largest_fitting_prefix_end(text, start, budget, length_function)
        if fitting_end == text_length:
            cut = fitting_end
        else:
            # Prefer the largest whitespace boundary in the bounded prefix.
            # If it contains no separator, split the long word or URL by character.
            boundary_index = bisect_right(boundaries, fitting_end) - 1
            cut = (
                boundaries[boundary_index]
                if boundary_index >= 0 and boundaries[boundary_index] > start
                else fitting_end
            )
        # Keep the separator at the end of the preceding part because stripping
        # it would silently change the original CSV cell when all continuation
        # parts are joined again.
        pieces.append(text[start:cut])
        start = cut
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
    columns = list(
        table_element.metadata.get("table_columns")
        or table_element.metadata.get("csv_columns")
        or _table_cells(header_lines[0])
    )
    first_row_number = int(
        table_element.metadata.get("table_row_start", table_element.metadata.get("csv_row_start", 2))
    )
    source_row_numbers = table_element.metadata.get("table_row_numbers") or table_element.metadata.get(
        "csv_row_numbers"
    )
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
        metadata = _tabular_row_metadata(table_element.metadata, list(current_row_numbers))
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
            # keeping an unexpected row whole is safer than silently corrupting it
            chunks.append(
                MDElement(
                    type="table",
                    content="\n".join([header_text, row]),
                    page_number=table_element.page_number,
                    metadata=_tabular_row_metadata(table_element.metadata, [row_number]),
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
        source_label = _tabular_label(table_element)
        base_cells[cell_index] = f"[{source_label} row {row_number}; {column_name} continuation 999/999]"
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
                f"[{source_label} row {row_number}; {column_name} continuation {part_number}/{len(parts)}] {part}"
            )
            continuation_row = _markdown_row(continuation_cells)
            chunks.append(
                MDElement(
                    type="table",
                    content="\n".join([header_text, continuation_row]),
                    page_number=table_element.page_number,
                    metadata={
                        **_tabular_row_metadata(table_element.metadata, [row_number]),
                        "csv_column": column_name,
                        "csv_part": part_number,
                        "csv_parts_total": len(parts),
                    },
                )
            )

    emit_current()

    # Retrieval must be able to reconstruct every logical row even when it was
    # split into more pieces than the general related-content limit.
    split_row_counts = Counter(
        int(chunk.metadata["csv_row_number"])
        for chunk in chunks
        if chunk.metadata
        and "csv_row_number" in chunk.metadata
        and ("csv_part" in chunk.metadata or "csv_column_group" in chunk.metadata)
    )
    for chunk in chunks:
        if not chunk.metadata or "csv_row_number" not in chunk.metadata:
            continue
        row_number = int(chunk.metadata["csv_row_number"])
        if row_number in split_row_counts:
            chunk.metadata["csv_row_chunks_total"] = split_row_counts[row_number]
    return chunks
