"""Deterministic reconstruction of logical table rows across adjacent PDF pages."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from core.config.table_reconstruction import TableReconstructionConfig
from core.indexing.structure_normalizer import (
    DocumentStructureNormalizer,
    LayoutCellEvidence,
    LayoutRowEvidence,
    LayoutTableEvidence,
    LayoutWord,
    PageLayoutEvidence,
    TableLayoutEvidenceProvider,
)
from core.indexing.table_text import render_table_legend, render_table_row
from core.models.document import (
    Document,
    DocumentType,
    NormalizationReport,
    PageBoundaryDecision,
    ProcessedDocument,
    SourceFragment,
    TableCellData,
    TableLegendData,
    TableLegendEntry,
    TableRowData,
    TextBlock,
)
from core.prompts.vlm_prompt_builder import wrap_caption

_MARKDOWN_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_HEADING_RE = re.compile(r"(?m)^\s*(?P<marks>#{1,6})\s+(?P<text>.+?)\s*$")
_SYNTHETIC_COLUMN_RE = re.compile(r"^col(?:umn)?\s*\d+$", re.IGNORECASE)
_ABBREVIATION_RE = re.compile(r"^([A-Z][A-Z0-9.-]{1,11})\s*[:=]\s*(.+)$")
_TABLE_TITLE_RE = re.compile(r"^(?:table(?:au)?\b|annexe\b|appendix\b)", re.IGNORECASE)
_HEADER_TERMS = {
    "area",
    "category",
    "catégorie",
    "city",
    "description",
    "document",
    "documents",
    "id",
    "intitulé",
    "libellé",
    "name",
    "number",
    "owner",
    "permit",
    "pièce",
    "pièces",
    "reference",
    "référence",
    "region",
    "status",
    "title",
    "titre",
    "type",
    "value",
    "valeur",
}


@dataclass(slots=True, frozen=True)
class _MarkdownCell:
    text: str
    start: int
    end: int


@dataclass(slots=True, frozen=True)
class _MarkdownRow:
    cells: tuple[_MarkdownCell, ...]
    start: int
    end: int
    separator: bool = False


@dataclass(slots=True, frozen=True)
class _MarkdownTable:
    rows: tuple[_MarkdownRow, ...]
    start: int
    end: int


@dataclass(slots=True)
class _TableAlignment:
    region: _AlignedRegion
    rows: list[list[tuple[SourceFragment | None, float]]]
    column_names: tuple[str, ...] | None = None
    parser_header: bool = False


@dataclass(slots=True, frozen=True)
class _AlignedRegion:
    source_block_index: int
    raw_start: int
    raw_end: int
    working_start: int
    working_end: int
    page_number: int
    kind: str
    source_ref: str | None = None
    confidence: float = 1.0


@dataclass(slots=True)
class _RowAlignment:
    cells: list[tuple[SourceFragment | None, float]]
    regions: list[_AlignedRegion]


@dataclass(slots=True)
class _MutableCell:
    column_index: int
    column_name: str | None
    parts: list[str] = field(default_factory=list)
    source_fragments: list[SourceFragment] = field(default_factory=list)
    assignment_confidence: float = 1.0
    column_span: int = 1
    row_span: int = 1
    inherited: bool = False
    inherited_from: tuple[int, int] | None = None
    explicit_empty: bool = False
    covered_by: tuple[int, int] | None = None

    def append(self, text: str, fragments: list[SourceFragment], confidence: float) -> None:
        clean = _clean_cell_text(text)
        if not clean:
            return
        prefix_length = sum(len(part) for part in self.parts) + (2 * len(self.parts))
        self.parts.append(clean)
        for fragment in fragments:
            fragment_length = fragment.text_end - fragment.text_start if fragment.text_end is not None else len(clean)
            self.source_fragments.append(
                fragment.model_copy(
                    update={
                        "text_start": prefix_length,
                        "text_end": prefix_length + max(fragment_length, len(clean)),
                    }
                )
            )
        self.assignment_confidence = min(self.assignment_confidence, confidence)

    @property
    def text(self) -> str:
        return "\n\n".join(self.parts)

    def freeze(self) -> TableCellData:
        return TableCellData(
            column_index=self.column_index,
            column_name=self.column_name,
            text=self.text,
            source_fragments=self.source_fragments,
            assignment_confidence=self.assignment_confidence,
            column_span=self.column_span,
            row_span=self.row_span,
            inherited=self.inherited,
            inherited_from=self.inherited_from,
            explicit_empty=self.explicit_empty,
            covered_by=self.covered_by,
        )


@dataclass(slots=True)
class _MutableRow:
    table_id: str
    algorithm_version: str
    table_title: str | None
    section_path: list[str]
    scope_fragments: list[SourceFragment]
    cells: list[_MutableCell]
    page_start: int
    page_end: int
    insertion_block_index: int
    insertion_offset: int
    row_index: int
    boundary_decisions: list[PageBoundaryDecision] = field(default_factory=list)

    def merge(
        self,
        row: LayoutRowEvidence,
        aligned_cells: list[tuple[SourceFragment | None, float]],
        decision: PageBoundaryDecision,
    ) -> None:
        for cell_evidence, (fragment, confidence), target in zip(row.cells, aligned_cells, self.cells, strict=True):
            fragments = [fragment] if fragment is not None else []
            target.append(cell_evidence.text, fragments, confidence)
        self.page_end = max(self.page_end, decision.next_page)
        self.boundary_decisions.append(decision)

    def freeze(self, identity_columns: tuple[int, ...]) -> TableRowData:
        identity = "\x1f".join(self.cells[index].text for index in identity_columns)
        row_hash = hashlib.sha256(
            (
                f"{self.table_id}\x1e{identity}\x1e{self.page_start}\x1e"
                f"{self.row_index}\x1e{self.insertion_block_index}\x1e{self.insertion_offset}"
            ).encode()
        ).hexdigest()[:20]
        return TableRowData(
            table_id=self.table_id,
            row_id=f"row-{row_hash}",
            algorithm_version=self.algorithm_version,
            table_title=self.table_title,
            section_path=self.section_path,
            scope_fragments=self.scope_fragments,
            cells=[cell.freeze() for cell in self.cells],
            identity_columns=list(identity_columns),
            row_index=self.row_index,
            page_start=self.page_start,
            page_end=self.page_end,
            boundary_decisions=self.boundary_decisions,
        )


@dataclass(slots=True)
class _NormalizedChain:
    rows: list[_MutableRow]
    identity_columns: tuple[int, ...]
    consumed_regions: list[_AlignedRegion]
    decisions: list[PageBoundaryDecision]
    used_tables: set[tuple[int, int]]
    fallback_reasons: list[str]
    legends: list[_LegendInsertion] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class _LegendInsertion:
    legend: TableLegendData
    insertion_block_index: int
    insertion_offset: int


@dataclass(slots=True, frozen=True)
class _ScopeContext:
    section_path: tuple[str, ...]
    table_title: str | None
    source_fragments: tuple[SourceFragment, ...]
    title_fragment: SourceFragment | None = None


def _clean_cell_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _normalised_alnum_with_offsets(text: str) -> tuple[str, list[int]]:
    ignored = [False] * len(text)
    for match in re.finditer(r"<[^>]*>", text):
        for index in range(match.start(), match.end()):
            ignored[index] = True

    chars: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        if ignored[index]:
            continue
        for normalized in unicodedata.normalize("NFKC", character).casefold():
            if normalized.isalnum():
                chars.append(normalized)
                offsets.append(index)
    return "".join(chars), offsets


def _normalised_alnum(text: str) -> str:
    return _normalised_alnum_with_offsets(text)[0]


def _similarity(expected: str, actual: str) -> float:
    left = _normalised_alnum(expected)
    right = _normalised_alnum(actual)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _find_normalised_range(needle: str, haystack: str) -> tuple[int, int, float] | None:
    normalized_needle = _normalised_alnum(needle)
    normalized_haystack, offsets = _normalised_alnum_with_offsets(haystack)
    if not normalized_needle or not normalized_haystack:
        return None
    positions: list[int] = []
    start = 0
    while True:
        found = normalized_haystack.find(normalized_needle, start)
        if found < 0:
            break
        positions.append(found)
        start = found + 1
        if len(positions) > 1:
            break
    if len(positions) != 1:
        return None
    normalized_start = positions[0]
    normalized_end = normalized_start + len(normalized_needle) - 1
    return offsets[normalized_start], offsets[normalized_end] + 1, 1.0


def _split_markdown_row(line: str, line_start: int) -> _MarkdownRow | None:
    stripped_start = len(line) - len(line.lstrip())
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None

    content_start = line_start + stripped_start
    pipes: list[int] = []
    escaped = False
    for index, character in enumerate(stripped):
        if character == "\\" and not escaped:
            escaped = True
            continue
        if character == "|" and not escaped:
            pipes.append(index)
        escaped = False
    if len(pipes) < 2:
        return None

    cells: list[_MarkdownCell] = []
    for left, right in zip(pipes, pipes[1:], strict=False):
        raw = stripped[left + 1 : right]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        start = content_start + left + 1 + leading
        end = content_start + left + 1 + trailing
        cells.append(_MarkdownCell(text=raw.strip(), start=start, end=max(start, end)))

    separator = bool(cells) and all(_MARKDOWN_SEPARATOR_RE.fullmatch(cell.text.strip()) for cell in cells)
    return _MarkdownRow(
        cells=tuple(cells),
        start=content_start,
        end=content_start + len(stripped),
        separator=separator,
    )


def _markdown_tables(text: str) -> list[_MarkdownTable]:
    tables: list[_MarkdownTable] = []
    current: list[_MarkdownRow] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        row = _split_markdown_row(line.rstrip("\r\n"), offset)
        if row is None:
            if current:
                if any(candidate.separator for candidate in current):
                    tables.append(_MarkdownTable(tuple(current), current[0].start, current[-1].end))
                current = []
        else:
            current.append(row)
        offset += len(line)
    if current and any(candidate.separator for candidate in current):
        tables.append(_MarkdownTable(tuple(current), current[0].start, current[-1].end))
    return tables


def _reliable_parser_header(row: _MarkdownRow) -> bool:
    """Reject synthetic continuation rows that only satisfy Markdown syntax."""
    real_labels = [
        cell.text.strip()
        for cell in row.cells
        if cell.text.strip()
        and not _SYNTHETIC_COLUMN_RE.fullmatch(cell.text.strip())
        and len(_normalised_alnum(cell.text)) <= 80
    ]
    return len(real_labels) >= max(2, (len(row.cells) + 1) // 2) and not any(
        re.search(r"\d", label) for label in real_labels
    )


def _parser_header_row(table: _MarkdownTable) -> _MarkdownRow | None:
    for row, next_row in zip(table.rows, table.rows[1:], strict=False):
        if not row.separator and next_row.separator and _reliable_parser_header(row):
            return row
    return None


def _unmatched_parser_rows_are_redundant(
    *,
    block_text: str,
    table: _MarkdownTable,
    content_rows: list[_MarkdownRow],
    matched_rows: list[_MarkdownRow],
) -> bool:
    unmatched = list(content_rows)
    for matched in matched_rows:
        for index, candidate in enumerate(unmatched):
            if candidate is matched:
                unmatched.pop(index)
                break

    outside = f"{block_text[: table.start]}\n{block_text[table.end :]}"
    for row in unmatched:
        text = " ".join(cell.text for cell in row.cells)
        if len(_normalised_alnum(text)) < 40:
            return False
        duplicated = _find_normalised_range(text, outside) is not None or any(
            _similarity(text, paragraph) >= 0.90 for paragraph in re.split(r"\n{2,}", outside) if paragraph.strip()
        )
        if not duplicated:
            return False
    return True


def _unique_exact_range(needle: str, haystack: str) -> tuple[int, int] | None:
    if not needle:
        return None
    start = haystack.find(needle)
    if start < 0 or haystack.find(needle, start + 1) >= 0:
        return None
    return start, start + len(needle)


def _image_regions(
    processed_document: ProcessedDocument,
    page_number: int,
) -> list[tuple[_AlignedRegion, str]]:
    raw_blocks = processed_document.raw_text_blocks or []
    regions: list[tuple[_AlignedRegion, str]] = []
    for image in processed_document.images:
        if image.page_number != page_number or not image.caption:
            continue
        markdown_ref = image.metadata.get("markdown_ref")
        if not isinstance(markdown_ref, str) or not markdown_ref:
            continue

        raw_matches: list[tuple[int, int, int]] = []
        for block_index, block in enumerate(raw_blocks):
            if block.page_number != page_number:
                continue
            found = _unique_exact_range(markdown_ref, block.text)
            if found is not None:
                raw_matches.append((block_index, found[0], found[1]))
        if len(raw_matches) != 1:
            continue

        block_index, raw_start, raw_end = raw_matches[0]
        if block_index >= len(processed_document.text_blocks):
            continue
        working = processed_document.text_blocks[block_index].text
        wrapped_caption = wrap_caption(image.caption)
        working_range = _unique_exact_range(wrapped_caption, working)
        if working_range is None:
            working_range = _unique_exact_range(markdown_ref, working)
        if working_range is None:
            continue

        source_ref = image.metadata.get("marker_key")
        regions.append(
            (
                _AlignedRegion(
                    source_block_index=block_index,
                    raw_start=raw_start,
                    raw_end=raw_end,
                    working_start=working_range[0],
                    working_end=working_range[1],
                    page_number=page_number,
                    kind="image_caption",
                    source_ref=str(source_ref or markdown_ref),
                ),
                image.caption,
            )
        )
    return regions


def _map_parser_region(
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    block_index: int,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    raw_blocks = processed_document.raw_text_blocks or []
    if block_index >= len(raw_blocks) or block_index >= len(processed_document.text_blocks):
        return None
    raw_block = raw_blocks[block_index]
    working_block = processed_document.text_blocks[block_index]
    direct = _map_interval(raw_block.text, working_block.text, start, end)
    if direct is not None:
        return direct

    replacements = sorted(
        (
            region
            for region, _ in image_regions.get(raw_block.page_number or 0, [])
            if region.source_block_index == block_index
        ),
        key=lambda region: region.raw_start,
    )
    if not replacements:
        return None

    cursor = 0
    rebuilt: list[str] = []
    for region in replacements:
        if region.raw_start < cursor:
            return None
        rebuilt.append(raw_block.text[cursor : region.raw_start])
        rebuilt.append(working_block.text[region.working_start : region.working_end])
        cursor = region.raw_end
    rebuilt.append(raw_block.text[cursor:])
    if "".join(rebuilt) != working_block.text:
        return None

    def map_offset(offset: int) -> int | None:
        delta = 0
        for region in replacements:
            if region.raw_start < offset < region.raw_end:
                return None
            if region.raw_end <= offset:
                delta += (region.working_end - region.working_start) - (region.raw_end - region.raw_start)
        return offset + delta

    mapped_start = map_offset(start)
    mapped_end = map_offset(end)
    if mapped_start is None or mapped_end is None:
        return None
    return mapped_start, mapped_end


def _evidence_overlap(expected: str, actual: str) -> float:
    normalized_expected = _normalised_alnum(expected)
    normalized_actual = _normalised_alnum(actual)
    if not normalized_expected or not normalized_actual:
        return 0.0
    width = 5

    def shingles(value: str) -> Counter[str]:
        if len(value) <= width:
            return Counter({value: 1})
        return Counter(value[index : index + width] for index in range(len(value) - width + 1))

    expected_shingles = shingles(normalized_expected)
    actual_shingles = shingles(normalized_actual)
    matched = sum((expected_shingles & actual_shingles).values())
    return min(
        matched / expected_shingles.total(),
        matched / actual_shingles.total(),
    )


def _match_image_region(
    expected: str,
    page_number: int,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    threshold: float,
) -> _AlignedRegion | None:
    # Short labels are too easy to associate with an unrelated page image.
    if len(_normalised_alnum(expected)) < 80:
        return None
    candidates = [
        (score, region)
        for region, caption in image_regions.get(page_number, [])
        if (score := _evidence_overlap(expected, caption)) >= threshold
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.05:
        return None
    score, region = candidates[0]
    return _AlignedRegion(
        source_block_index=region.source_block_index,
        raw_start=region.raw_start,
        raw_end=region.raw_end,
        working_start=region.working_start,
        working_end=region.working_end,
        page_number=region.page_number,
        kind=region.kind,
        source_ref=region.source_ref,
        confidence=score,
    )


def _layout_fragment(
    cell: LayoutCellEvidence,
    region: _AlignedRegion,
    evidence_provider: str,
) -> SourceFragment | None:
    if cell.bbox is None:
        return None
    return SourceFragment(
        source_block_index=region.source_block_index,
        page_number=region.page_number,
        char_start=region.raw_start,
        char_end=region.raw_end,
        source_kind="pdf_layout",
        evidence_provider=evidence_provider,
        source_ref=region.source_ref,
        bbox=cell.bbox,
        text_start=0,
        text_end=max(1, len(_clean_cell_text(cell.text))),
    )


def _markdown_cell_alignment(
    evidence_cell: LayoutCellEvidence,
    markdown_cell: _MarkdownCell,
    *,
    page_number: int,
    block_index: int,
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    threshold: float,
) -> tuple[float, _AlignedRegion | None]:
    if not evidence_cell.text.strip():
        confidence = (
            1.0 if not markdown_cell.text.strip() or _SYNTHETIC_COLUMN_RE.fullmatch(markdown_cell.text.strip()) else 0.0
        )
        return confidence, None

    # Table normalization is allowed to repair spacing and punctuation, but it
    # must never replace parser-only content with a merely similar layout
    # value. Require complete alphanumeric agreement for parser text. Image
    # captions remain a separately evidenced replacement path below.
    confidence = 1.0 if _normalised_alnum(evidence_cell.text) == _normalised_alnum(markdown_cell.text) else 0.0
    if confidence >= threshold and markdown_cell.end > markdown_cell.start:
        return confidence, None

    image_region = _match_image_region(
        evidence_cell.text,
        page_number,
        image_regions,
        threshold,
    )
    if (
        image_region is None
        or image_region.source_block_index != block_index
        or image_region.raw_start < markdown_cell.start
        or image_region.raw_end > markdown_cell.end
    ):
        return confidence, None
    return image_region.confidence, image_region


def _align_plain_table(
    table: LayoutTableEvidence,
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
) -> _TableAlignment | None:
    """Align a layout table to parser text that contains no Markdown grid.

    This path is deliberately strict: every non-empty layout cell must occur
    in order, and the complete consumed parser range must contain exactly
    those cell values after alphanumeric normalization.
    """
    raw_blocks = processed_document.raw_text_blocks or []
    values = [cell.text for row in table.rows for cell in row.cells if cell.text.strip()]
    normalized_values = [_normalised_alnum(value) for value in values]
    if not values or any(not value for value in normalized_values):
        return None
    normalized_table = "".join(normalized_values)

    candidates: list[tuple[int, int, int, list[tuple[int, int]]]] = []
    for block_index, block in enumerate(raw_blocks):
        if block.page_number != table.page_number:
            continue
        markdown_ranges = [(candidate.start, candidate.end) for candidate in _markdown_tables(block.text)]
        segment_starts = [0, *(end for _, end in markdown_ranges)]
        segment_ends = [*(start for start, _ in markdown_ranges), len(block.text)]
        for segment_start, segment_end in zip(
            segment_starts,
            segment_ends,
            strict=True,
        ):
            segment = block.text[segment_start:segment_end]
            normalized_block, offsets = _normalised_alnum_with_offsets(segment)
            starts: list[int] = []
            cursor = 0
            while True:
                found = normalized_block.find(normalized_table, cursor)
                if found < 0:
                    break
                starts.append(found)
                cursor = found + 1
                if len(starts) > 1:
                    break
            if len(starts) != 1:
                continue
            normalized_ranges: list[tuple[int, int]] = []
            cursor = starts[0]
            for value in normalized_values:
                normalized_ranges.append((cursor, cursor + len(value)))
                cursor += len(value)
            char_ranges = [
                (
                    segment_start + offsets[start],
                    segment_start + offsets[end - 1] + 1,
                )
                for start, end in normalized_ranges
            ]
            raw_start = char_ranges[0][0]
            raw_end = char_ranges[-1][1]
            mapped = _map_parser_region(
                processed_document,
                image_regions,
                block_index,
                raw_start,
                raw_end,
            )
            if mapped is not None:
                candidates.append((block_index, raw_start, raw_end, char_ranges))

    if len(candidates) != 1:
        return None
    block_index, raw_start, raw_end, char_ranges = candidates[0]
    mapped = _map_parser_region(
        processed_document,
        image_regions,
        block_index,
        raw_start,
        raw_end,
    )
    if mapped is None:
        return None

    range_cursor = 0
    aligned_rows: list[list[tuple[SourceFragment | None, float]]] = []
    for row in table.rows:
        aligned_cells: list[tuple[SourceFragment | None, float]] = []
        for cell in row.cells:
            if not cell.text.strip():
                aligned_cells.append((None, 1.0))
                continue
            start, end = char_ranges[range_cursor]
            range_cursor += 1
            aligned_cells.append(
                (
                    SourceFragment(
                        source_block_index=block_index,
                        page_number=table.page_number,
                        char_start=start,
                        char_end=end,
                        source_kind="parser_text",
                        bbox=cell.bbox,
                        text_start=0,
                        text_end=max(1, len(_clean_cell_text(cell.text))),
                    ),
                    1.0,
                )
            )
        aligned_rows.append(aligned_cells)

    return _TableAlignment(
        region=_AlignedRegion(
            source_block_index=block_index,
            raw_start=raw_start,
            raw_end=raw_end,
            working_start=mapped[0],
            working_end=mapped[1],
            page_number=table.page_number,
            kind="plain_table",
        ),
        rows=aligned_rows,
        column_names=_column_names(table.rows[0]),
    )


def _align_table(
    table: LayoutTableEvidence,
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    evidence_provider: str,
    threshold: float,
) -> _TableAlignment | None:
    raw_blocks = processed_document.raw_text_blocks or []
    candidates: list[tuple[float, int, _MarkdownTable, list[_MarkdownRow], bool]] = []
    for block_index, block in enumerate(raw_blocks):
        if block.page_number != table.page_number:
            continue
        markdown_tables = _markdown_tables(block.text)
        for markdown_table in markdown_tables:
            content_rows = [row for row in markdown_table.rows if not row.separator]
            parser_header_row = _parser_header_row(markdown_table)
            matched: list[_MarkdownRow] = []
            cursor = 0
            scores: list[float] = []
            for evidence_row in table.rows:
                best_score = 0.0
                best_index: int | None = None
                for row_index in range(cursor, len(content_rows)):
                    markdown_row = content_rows[row_index]
                    if len(evidence_row.cells) != len(markdown_row.cells):
                        continue
                    cell_scores = [
                        _markdown_cell_alignment(
                            evidence_cell,
                            markdown_cell,
                            page_number=table.page_number,
                            block_index=block_index,
                            processed_document=processed_document,
                            image_regions=image_regions,
                            threshold=threshold,
                        )[0]
                        for evidence_cell, markdown_cell in zip(
                            evidence_row.cells,
                            markdown_row.cells,
                            strict=True,
                        )
                    ]
                    if any(cell_score < threshold for cell_score in cell_scores):
                        continue
                    score = sum(cell_scores) / len(cell_scores) if cell_scores else 0.0
                    if score > best_score:
                        best_score = score
                        best_index = row_index
                if best_index is None:
                    break
                matched.append(content_rows[best_index])
                scores.append(best_score)
                cursor = best_index + 1
            if len(matched) == len(table.rows) and _unmatched_parser_rows_are_redundant(
                block_text=block.text,
                table=markdown_table,
                content_rows=content_rows,
                matched_rows=matched,
            ):
                candidates.append(
                    (
                        sum(scores) / len(scores),
                        block_index,
                        markdown_table,
                        matched,
                        bool(matched and matched[0] is parser_header_row),
                    )
                )

    if not candidates:
        table_text = "\n".join(cell.text for row in table.rows for cell in row.cells if cell.text.strip())
        image_region = _match_image_region(table_text, table.page_number, image_regions, threshold)
        if image_region is None:
            return _align_plain_table(
                table,
                processed_document,
                image_regions,
            )
        return _TableAlignment(
            region=image_region,
            rows=[
                [
                    (None, 1.0)
                    if not cell.text.strip()
                    else (
                        _layout_fragment(cell, image_region, evidence_provider),
                        image_region.confidence,
                    )
                    for cell in row.cells
                ]
                for row in table.rows
            ],
        )

    score, block_index, markdown_table, matched_rows, parser_header = max(
        candidates,
        key=lambda candidate: candidate[0],
    )
    if score < threshold:
        return None
    if block_index >= len(processed_document.text_blocks):
        return None
    mapped_table = _map_parser_region(
        processed_document,
        image_regions,
        block_index,
        markdown_table.start,
        markdown_table.end,
    )
    if mapped_table is None:
        return None
    table_region = _AlignedRegion(
        source_block_index=block_index,
        raw_start=markdown_table.start,
        raw_end=markdown_table.end,
        working_start=mapped_table[0],
        working_end=mapped_table[1],
        page_number=table.page_number,
        kind="markdown_table",
        confidence=score,
    )

    aligned_rows: list[list[tuple[SourceFragment | None, float]]] = []
    for evidence_row, markdown_row in zip(table.rows, matched_rows, strict=True):
        aligned_cells: list[tuple[SourceFragment | None, float]] = []
        for evidence_cell, markdown_cell in zip(evidence_row.cells, markdown_row.cells, strict=True):
            if not evidence_cell.text.strip():
                confidence, _ = _markdown_cell_alignment(
                    evidence_cell,
                    markdown_cell,
                    page_number=table.page_number,
                    block_index=block_index,
                    processed_document=processed_document,
                    image_regions=image_regions,
                    threshold=threshold,
                )
                aligned_cells.append((None, confidence))
                continue
            confidence, image_region = _markdown_cell_alignment(
                evidence_cell,
                markdown_cell,
                page_number=table.page_number,
                block_index=block_index,
                processed_document=processed_document,
                image_regions=image_regions,
                threshold=threshold,
            )
            if image_region is not None:
                aligned_cells.append(
                    (
                        _layout_fragment(evidence_cell, image_region, evidence_provider),
                        image_region.confidence,
                    )
                )
                continue
            if confidence < threshold or markdown_cell.end <= markdown_cell.start:
                aligned_cells.append((None, confidence))
                continue
            uses_layout_text = evidence_cell.bbox is not None and _clean_cell_text(
                evidence_cell.text
            ) != _clean_cell_text(markdown_cell.text)
            aligned_cells.append(
                (
                    SourceFragment(
                        source_block_index=block_index,
                        page_number=table.page_number,
                        char_start=markdown_cell.start,
                        char_end=markdown_cell.end,
                        source_kind=("pdf_layout" if uses_layout_text else "parser_text"),
                        evidence_provider=(evidence_provider if uses_layout_text else None),
                        bbox=evidence_cell.bbox,
                        text_start=0,
                        text_end=max(1, len(_clean_cell_text(evidence_cell.text))),
                    ),
                    confidence,
                )
            )
        aligned_rows.append(aligned_cells)

    column_names: list[str] = []
    for column_index, evidence_cell in enumerate(table.rows[0].cells):
        # Layout words preserve the visual boundary between a concise header and
        # any legend printed beneath it. Parser Markdown may collapse those
        # lines into one key even when its alphanumeric content still aligns.
        column_names.append(_concise_column_name(evidence_cell.text, column_index))

    return _TableAlignment(
        region=table_region,
        rows=aligned_rows,
        column_names=tuple(column_names),
        parser_header=parser_header,
    )


def _looks_like_header(
    row: LayoutRowEvidence,
    following_row: LayoutRowEvidence | None = None,
) -> bool:
    nonempty = sum(bool(cell.text.strip()) for cell in row.cells)
    if nonempty < max(2, (len(row.cells) + 1) // 2):
        return False
    matched_labels = sum(
        bool(set(re.findall(r"\w+", cell.text.casefold())) & _HEADER_TERMS) and len(_normalised_alnum(cell.text)) <= 200
        for cell in row.cells
    )
    if matched_labels >= 2:
        return True

    # Generic column labels such as ``aa | bb | cc`` have no semantic header
    # vocabulary. Treat them as a header only when the next row supplies a
    # strong data-type contrast. This preserves the first row of headerless
    # tables such as ``22 | Paris | Active``.
    if following_row is None or len(row.cells) != len(following_row.cells):
        return False
    labels = [_clean_cell_text(cell.text) for cell in row.cells]
    if not all(
        label and len(label) <= 40 and re.fullmatch(r"[\wÀ-ÖØ-öø-ÿ .()/+-]+", label) and not re.search(r"\d", label)
        for label in labels
    ):
        return False
    return any(
        bool(re.search(r"\d", following.text)) and not bool(re.search(r"\d", header.text))
        for header, following in zip(row.cells, following_row.cells, strict=True)
    )


def _content_column(rows: tuple[LayoutRowEvidence, ...]) -> int:
    column_count = len(rows[0].cells)
    lengths = [0] * column_count
    for row in rows:
        for cell in row.cells:
            lengths[cell.column_index] += len(_normalised_alnum(cell.text))
    return max(range(column_count), key=lambda index: lengths[index])


def _concise_column_name(text: str, column_index: int) -> str:
    clean = _clean_cell_text(text)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    concise = lines[0] if lines else ""
    for continuation in lines[1:]:
        candidate = f"{concise} {continuation}".strip()
        if ":" in continuation or len(candidate) > 80:
            break
        concise = candidate
    return concise or f"Column {column_index + 1}"


def _column_names(header: LayoutRowEvidence) -> tuple[str, ...]:
    return tuple(_concise_column_name(cell.text, cell.column_index) for cell in header.cells)


def _is_repeated_header(
    row: LayoutRowEvidence,
    column_names: tuple[str, ...],
) -> bool:
    """Recognize only complete, exact repetitions of the established header."""
    if len(row.cells) != len(column_names):
        return False
    return all(
        bool(_normalised_alnum(column_name))
        and _normalised_alnum(_concise_column_name(cell.text, cell.column_index)) == _normalised_alnum(column_name)
        for cell, column_name in zip(row.cells, column_names, strict=True)
    )


def _legend_entries(
    header: LayoutRowEvidence,
    aligned_header: list[tuple[SourceFragment | None, float]],
) -> list[TableLegendEntry]:
    entries: list[TableLegendEntry] = []
    seen: set[str] = set()
    for cell, (fragment, _) in zip(header.cells, aligned_header, strict=True):
        lines = [line.strip() for line in _clean_cell_text(cell.text).splitlines() if line.strip()]
        current_abbreviation: str | None = None
        current_meaning: list[str] = []

        def flush() -> None:
            nonlocal current_abbreviation, current_meaning
            if current_abbreviation is None:
                return
            meaning = " ".join(current_meaning).strip()
            if meaning and current_abbreviation not in seen:
                fragments = [fragment.model_copy(deep=True)] if fragment is not None else []
                entries.append(
                    TableLegendEntry(
                        abbreviation=current_abbreviation,
                        meaning=meaning,
                        source_fragments=fragments,
                    )
                )
                seen.add(current_abbreviation)
            current_abbreviation = None
            current_meaning = []

        for line in lines:
            match = _ABBREVIATION_RE.fullmatch(line)
            if match is not None:
                flush()
                current_abbreviation = match.group(1)
                current_meaning = [match.group(2)]
            elif current_abbreviation is not None:
                current_meaning.append(line)
        flush()
    return entries


def _spans_are_unambiguous(table: LayoutTableEvidence, *, header_rows: int) -> bool:
    if any(cell.slot_state == "unknown" for row in table.rows for cell in row.cells):
        return False
    return not any(
        cell.column_span > 1 or cell.row_span > 1 or cell.slot_state == "covered"
        for row in table.rows[:header_rows]
        for cell in row.cells
    )


def _apply_merged_cell_inheritance(
    rows: list[_MutableRow],
    evidence_rows: tuple[LayoutRowEvidence, ...],
    *,
    evidence_row_offset: int,
) -> bool:
    """Resolve page-local covered slots without guessing across boundaries."""
    for logical_index, (row, evidence) in enumerate(zip(rows, evidence_rows, strict=True)):
        for cell_evidence, target in zip(evidence.cells, row.cells, strict=True):
            if cell_evidence.slot_state != "covered":
                continue
            if cell_evidence.covered_by is None:
                return False
            anchor_evidence_row, anchor_column = cell_evidence.covered_by
            anchor_logical_index = anchor_evidence_row - evidence_row_offset
            if anchor_logical_index == logical_index:
                target.covered_by = (row.row_index, anchor_column)
                continue
            if not 0 <= anchor_logical_index < logical_index:
                return False
            anchor = rows[anchor_logical_index].cells[anchor_column]
            if not anchor.text or anchor.row_span <= logical_index - anchor_logical_index:
                return False
            target.parts = list(anchor.parts)
            target.source_fragments = [fragment.model_copy(deep=True) for fragment in anchor.source_fragments]
            target.assignment_confidence = anchor.assignment_confidence
            target.column_span = anchor.column_span
            target.row_span = 1
            target.inherited = True
            target.inherited_from = (
                rows[anchor_logical_index].row_index,
                anchor_column,
            )
            target.explicit_empty = False
            target.covered_by = None
    return True


def _table_geometry_confidence(
    expected: tuple[tuple[float, float], ...],
    actual: tuple[tuple[float, float], ...],
) -> float:
    if len(expected) != len(actual):
        return 0.0
    delta = max(
        abs(left - right)
        for pair_a, pair_b in zip(expected, actual, strict=True)
        for left, right in zip(pair_a, pair_b, strict=True)
    )
    return max(0.0, 1.0 - (delta / 0.10))


def _compatible_pages(previous: PageLayoutEvidence, following: PageLayoutEvidence) -> bool:
    previous_landscape = previous.width > previous.height
    following_landscape = following.width > following.height
    if previous_landscape != following_landscape:
        return False
    previous_ratio = previous.width / previous.height
    following_ratio = following.width / following.height
    return abs(previous_ratio - following_ratio) / max(previous_ratio, following_ratio) <= 0.05


def _page_starts_with_heading(raw_blocks: list[TextBlock], page_number: int) -> bool:
    for block in raw_blocks:
        if block.page_number != page_number:
            continue
        first_content = block.text.lstrip()
        if first_content:
            return bool(re.match(r"^#{1,6}\s+", first_content))
    return False


def _has_parser_content_before_table(
    raw_blocks: list[TextBlock],
    page_number: int,
) -> bool:
    preceding_content = False
    for block in raw_blocks:
        if block.page_number != page_number:
            continue
        tables = _markdown_tables(block.text)
        if tables:
            return preceding_content or bool(block.text[: tables[0].start].strip())
        preceding_content = preceding_content or bool(block.text.strip())
    return False


def _page_edge_confidences(
    previous_bottom: float,
    next_top: float,
) -> tuple[float, float]:
    """Score how strongly the two regions touch their respective page edges."""
    bottom = 0.90 + 0.10 * min(1.0, max(0.0, (previous_bottom - 0.82) / 0.18))
    top = 0.90 + 0.10 * min(1.0, max(0.0, (0.15 - next_top) / 0.15))
    return bottom, top


def _has_content_before_regions(
    raw_blocks: list[TextBlock],
    regions: list[_AlignedRegion],
) -> bool:
    """Reject continuation evidence preceded by unaccounted page content."""
    if not regions:
        return True
    first = min(
        regions,
        key=lambda region: (region.source_block_index, region.raw_start),
    )
    for block_index, block in enumerate(raw_blocks):
        if block.page_number != first.page_number:
            continue
        if block_index < first.source_block_index and block.text.strip():
            return True
        if block_index == first.source_block_index:
            return bool(block.text[: first.raw_start].strip())
    return True


def _words_to_text(words: list[LayoutWord]) -> str:
    lines: list[str] = []
    current_key: tuple[int, int] | None = None
    current_words: list[str] = []
    previous_block: int | None = None
    for word in sorted(words, key=lambda item: (item.block_number, item.line_number, item.word_number)):
        key = (word.block_number, word.line_number)
        if key != current_key:
            if current_words:
                lines.append(" ".join(current_words))
                if previous_block is not None and word.block_number != previous_block:
                    lines.append("")
            current_words = []
            current_key = key
            previous_block = word.block_number
        current_words.append(word.text)
    if current_words:
        lines.append(" ".join(current_words))
    return "\n".join(lines).strip()


def _sparse_continuation(
    page: PageLayoutEvidence,
    column_bounds: tuple[tuple[float, float], ...],
    identity_columns: tuple[int, ...],
) -> LayoutRowEvidence | None:
    grouped: list[list[LayoutWord]] = [[] for _ in column_bounds]
    for word in page.words:
        x0, y0, x1, y1 = word.bbox
        if y1 >= 0.90:
            continue
        center = (x0 + x1) / 2
        for column_index, (left, right) in enumerate(column_bounds):
            if left <= center <= right:
                grouped[column_index].append(word)
                break

    if any(grouped[index] for index in identity_columns):
        return None
    populated = [index for index, words in enumerate(grouped) if words]
    if not populated:
        return None
    all_words = [word for words in grouped for word in words]
    if min(word.bbox[1] for word in all_words) > 0.12:
        return None

    cells: list[LayoutCellEvidence] = []
    for column_index, words in enumerate(grouped):
        if words:
            bbox = (
                min(word.bbox[0] for word in words),
                min(word.bbox[1] for word in words),
                max(word.bbox[2] for word in words),
                max(word.bbox[3] for word in words),
            )
            text = _words_to_text(words)
        else:
            bbox = None
            text = ""
        cells.append(LayoutCellEvidence(column_index=column_index, text=text, bbox=bbox))

    return LayoutRowEvidence(
        cells=tuple(cells),
        bbox=(
            min(word.bbox[0] for word in all_words),
            min(word.bbox[1] for word in all_words),
            max(word.bbox[2] for word in all_words),
            max(word.bbox[3] for word in all_words),
        ),
    )


def _align_sparse_row(
    row: LayoutRowEvidence,
    page_number: int,
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    evidence_provider: str,
    threshold: float,
) -> _RowAlignment | None:
    raw_blocks = processed_document.raw_text_blocks or []
    aligned: list[tuple[SourceFragment | None, float]] = []
    regions: list[_AlignedRegion] = []
    direct_alignment_failed = False
    for cell in row.cells:
        if not cell.text:
            aligned.append((None, 1.0))
            continue
        matches: list[tuple[int, int, int, float]] = []
        for block_index, block in enumerate(raw_blocks):
            if block.page_number != page_number:
                continue
            found = _find_normalised_range(cell.text, block.text)
            if found is not None:
                matches.append((block_index, *found))
        if len(matches) != 1:
            direct_alignment_failed = True
            break
        block_index, start, end, confidence = matches[0]
        source_text = raw_blocks[block_index].text
        while start > 0 and not source_text[start - 1].isalnum():
            start -= 1
        while end < len(source_text) and not source_text[end].isalnum():
            end += 1
        if block_index >= len(processed_document.text_blocks):
            direct_alignment_failed = True
            break
        mapped = _map_parser_region(
            processed_document,
            image_regions,
            block_index,
            start,
            end,
        )
        if mapped is None:
            direct_alignment_failed = True
            break
        aligned.append(
            (
                SourceFragment(
                    source_block_index=block_index,
                    page_number=page_number,
                    char_start=start,
                    char_end=end,
                    bbox=cell.bbox,
                    text_start=0,
                    text_end=max(1, len(_clean_cell_text(cell.text))),
                ),
                confidence,
            )
        )
        regions.append(
            _AlignedRegion(
                source_block_index=block_index,
                raw_start=start,
                raw_end=end,
                working_start=mapped[0],
                working_end=mapped[1],
                page_number=page_number,
                kind="parser_text",
                confidence=confidence,
            )
        )
    if not direct_alignment_failed:
        return _RowAlignment(cells=aligned, regions=regions)

    expected = "\n".join(cell.text for cell in row.cells if cell.text.strip())
    image_region = _match_image_region(
        expected,
        page_number,
        image_regions,
        threshold,
    )
    if image_region is None:
        return None
    return _RowAlignment(
        cells=[
            (None, 1.0)
            if not cell.text.strip()
            else (
                _layout_fragment(cell, image_region, evidence_provider),
                image_region.confidence,
            )
            for cell in row.cells
        ],
        regions=[image_region],
    )


def _scope_fragment(
    *,
    block: TextBlock,
    block_index: int,
    start: int,
    end: int,
    fallback_page: int,
    text: str,
) -> SourceFragment:
    return SourceFragment(
        source_block_index=block_index,
        page_number=block.page_number or fallback_page,
        char_start=start,
        char_end=end,
        text_start=0,
        text_end=max(1, len(text)),
    )


def _scope_context(
    raw_blocks: list[TextBlock],
    *,
    anchor_block_index: int,
    anchor_offset: int,
    fallback_page: int,
) -> _ScopeContext:
    """Resolve heading ancestry and a separately evidenced table caption."""
    headings: list[tuple[int, str, SourceFragment]] = []
    title_candidate: tuple[str, SourceFragment, int, int] | None = None
    last_heading_end = 0

    for block_index, block in enumerate(raw_blocks[: anchor_block_index + 1]):
        limit = anchor_offset if block_index == anchor_block_index else len(block.text)
        for match in _HEADING_RE.finditer(block.text[:limit]):
            level = len(match.group("marks"))
            raw_text = match.group("text")
            clean = _clean_cell_text(raw_text).strip("*_ ")
            if not clean:
                continue
            while headings and headings[-1][0] >= level:
                headings.pop()
            fragment = _scope_fragment(
                block=block,
                block_index=block_index,
                start=match.start("text"),
                end=match.end("text"),
                fallback_page=fallback_page,
                text=clean,
            )
            if _TABLE_TITLE_RE.match(clean):
                title_candidate = (clean, fragment, block_index, match.end())
            else:
                headings.append((level, clean, fragment))
                title_candidate = None
            if block_index == anchor_block_index:
                last_heading_end = match.end()

    anchor_block = raw_blocks[anchor_block_index]
    cursor = last_heading_end
    for line in anchor_block.text[last_heading_end:anchor_offset].splitlines(keepends=True):
        raw_line = line.rstrip("\r\n")
        stripped = raw_line.strip()
        clean = _clean_cell_text(stripped).strip("*_ ")
        if clean and len(clean) <= 120 and _TABLE_TITLE_RE.match(clean):
            leading = len(raw_line) - len(raw_line.lstrip())
            start = cursor + leading
            title_candidate = (
                clean,
                _scope_fragment(
                    block=anchor_block,
                    block_index=anchor_block_index,
                    start=start,
                    end=start + len(stripped),
                    fallback_page=fallback_page,
                    text=clean,
                ),
                anchor_block_index,
                cursor + len(line),
            )
        cursor += len(line)

    title: tuple[str, SourceFragment] | None = None
    if title_candidate is not None:
        clean, fragment, block_index, end = title_candidate
        intervening = [raw_blocks[block_index].text[end:]]
        intervening.extend(block.text for block in raw_blocks[block_index + 1 : anchor_block_index])
        if block_index != anchor_block_index:
            intervening.append(anchor_block.text[:anchor_offset])
        else:
            intervening[0] = anchor_block.text[end:anchor_offset]
        # Introductory prose between a caption and its first table is common.
        # The caption stops applying once another Markdown table has appeared,
        # preventing a later untitled table from inheriting a stale name.
        if not _markdown_tables("\n".join(intervening)):
            title = (clean, fragment)

    fragments = [fragment for _, _, fragment in headings]
    if title is not None:
        fragments.append(title[1])
    return _ScopeContext(
        section_path=tuple(text for _, text, _ in headings),
        table_title=title[0] if title is not None else None,
        source_fragments=tuple(fragments),
        title_fragment=title[1] if title is not None else None,
    )


def _row_from_evidence(
    *,
    table_id: str,
    algorithm_version: str,
    table_title: str | None,
    section_path: list[str],
    scope_fragments: list[SourceFragment],
    column_names: tuple[str, ...],
    evidence: LayoutRowEvidence,
    aligned: list[tuple[SourceFragment | None, float]],
    page_number: int,
    insertion_block_index: int,
    insertion_offset: int,
    row_index: int,
) -> _MutableRow:
    cells: list[_MutableCell] = []
    for name, cell_evidence, (fragment, confidence) in zip(column_names, evidence.cells, aligned, strict=True):
        cell = _MutableCell(
            column_index=cell_evidence.column_index,
            column_name=name,
            column_span=cell_evidence.column_span,
            row_span=cell_evidence.row_span,
            explicit_empty=cell_evidence.slot_state == "explicit_empty",
            covered_by=cell_evidence.covered_by,
        )
        cell.append(cell_evidence.text, [fragment] if fragment is not None else [], confidence)
        cells.append(cell)
    return _MutableRow(
        table_id=table_id,
        algorithm_version=algorithm_version,
        table_title=table_title,
        section_path=section_path,
        scope_fragments=scope_fragments,
        cells=cells,
        page_start=page_number,
        page_end=page_number,
        insertion_block_index=insertion_block_index,
        insertion_offset=insertion_offset,
        row_index=row_index,
    )


def _all_assignments_pass(
    row: LayoutRowEvidence,
    aligned: list[tuple[SourceFragment | None, float]],
    threshold: float,
) -> bool:
    return all(
        confidence >= threshold and (not cell.text.strip() or fragment is not None)
        for cell, (fragment, confidence) in zip(row.cells, aligned, strict=True)
    )


class DeterministicTableNormalizer(DocumentStructureNormalizer):
    """Reconstruct high-confidence adjacent-page row continuations."""

    def __init__(self, evidence_provider: TableLayoutEvidenceProvider) -> None:
        self._evidence_provider = evidence_provider

    async def normalize(
        self,
        document: Document,
        processed_document: ProcessedDocument,
        config: TableReconstructionConfig,
    ) -> ProcessedDocument:
        if config.mode == "disabled" or document.content_type is not DocumentType.PDF:
            return processed_document
        raw_blocks = processed_document.raw_text_blocks
        if not raw_blocks or not document.raw_bytes:
            return self._unchanged(processed_document, config, "raw parser blocks or PDF bytes are unavailable")

        table_pages = {
            block.page_number for block in raw_blocks if block.page_number is not None and _markdown_tables(block.text)
        }
        table_pages.update(
            page
            for page in await self._evidence_provider.discover(document)
            if 1 <= page <= processed_document.page_count
        )
        candidate_image_pages = {
            image.page_number
            for image in processed_document.images
            if image.page_number is not None
            and image.caption
            and len(_normalised_alnum(image.caption)) >= 80
            and image.metadata.get("markdown_ref")
        }
        image_regions = {
            page_number: regions
            for page_number in candidate_image_pages
            if (regions := _image_regions(processed_document, page_number))
        }
        table_pages.update(image_regions)
        if not table_pages:
            return self._unchanged(processed_document, config, "no parser-level table candidates were found")

        candidate_pages = {
            page
            for table_page in table_pages
            for page in (table_page - 1, table_page, table_page + 1)
            if 1 <= page <= processed_document.page_count
        }
        evidence = await self._evidence_provider.collect(document, candidate_pages)
        pages = {page.page_number: page for page in evidence}

        chains: list[_NormalizedChain] = []
        used_tables: set[tuple[int, int]] = set()
        claimed_captions: set[tuple[int, int, int]] = set()
        preserved_candidates: list[str] = []
        for page_number in sorted(pages):
            page = pages[page_number]
            for table_index, table in enumerate(page.tables):
                key = (page_number, table_index)
                if key in used_tables:
                    continue
                chain = await self._build_chain(
                    document,
                    table,
                    table_index,
                    pages,
                    processed_document,
                    image_regions,
                    config,
                    processed_document.page_count,
                    claimed_captions,
                )
                if chain is None:
                    preserved_candidates.append(
                        f"preserved table candidate {page_number}:{table_index} because a confidence or alignment gate failed"
                    )
                    continue
                chains.append(chain)
                used_tables.update(chain.used_tables)

        if not chains:
            return self._unchanged(processed_document, config, "no table candidate passed all confidence gates")

        rows: list[tuple[_MutableRow, tuple[int, ...]]] = []
        legends: list[_LegendInsertion] = []
        consumed: list[_AlignedRegion] = []
        decisions: list[PageBoundaryDecision] = []
        fallback_reasons: list[str] = list(preserved_candidates)
        for chain in chains:
            rows.extend((row, chain.identity_columns) for row in chain.rows)
            consumed.extend(chain.consumed_regions)
            decisions.extend(chain.decisions)
            fallback_reasons.extend(chain.fallback_reasons)
            legends.extend(chain.legends)

        if not _regions_are_disjoint(consumed):
            return self._unchanged(processed_document, config, "candidate table overlays overlap")

        normalized_blocks = _build_normalized_blocks(
            processed_document,
            rows,
            legends,
            consumed,
        )
        if normalized_blocks is None:
            return self._unchanged(processed_document, config, "raw and captioned block ranges could not be aligned")

        status = "partial_fallback" if fallback_reasons else "normalized"
        return processed_document.model_copy(
            update={
                "normalized_text_blocks": normalized_blocks,
                "normalization_report": NormalizationReport(
                    algorithm_version=config.algorithm_version,
                    status=status,
                    boundary_decisions=decisions,
                    reconstructed_row_count=len(rows),
                    fallback_reasons=fallback_reasons,
                ),
            }
        )

    async def _build_chain(
        self,
        document: Document,
        anchor: LayoutTableEvidence,
        anchor_index: int,
        pages: dict[int, PageLayoutEvidence],
        processed_document: ProcessedDocument,
        image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
        config: TableReconstructionConfig,
        page_count: int,
        claimed_captions: set[tuple[int, int, int]],
    ) -> _NormalizedChain | None:
        raw_blocks = processed_document.raw_text_blocks or []
        if not anchor.rows:
            return None

        alignment = _align_table(
            anchor,
            processed_document,
            image_regions,
            self._evidence_provider.provider_id,
            config.cell_assignment_min_confidence,
        )
        if alignment is None:
            return None
        has_header = alignment.parser_header or (
            len(anchor.rows) >= 2
            and _looks_like_header(
                anchor.rows[0],
                anchor.rows[1],
            )
        )
        if not has_header:
            first_nonempty = sum(bool(cell.text.strip()) for cell in anchor.rows[0].cells)
            if first_nonempty < max(2, (len(anchor.rows[0].cells) + 1) // 2):
                # A sparse leading row at the top of a page is likely a
                # continuation, not a safe headerless table anchor.
                return None
        header_rows = 1 if has_header else 0
        if not _spans_are_unambiguous(anchor, header_rows=header_rows):
            return None
        data_rows = anchor.rows[header_rows:]
        aligned_data = alignment.rows[header_rows:]
        if not data_rows:
            return None
        if any(
            not _all_assignments_pass(row, aligned, config.cell_assignment_min_confidence)
            for row, aligned in zip(data_rows, aligned_data, strict=True)
        ):
            return None

        column_names = (
            (alignment.column_names or _column_names(anchor.rows[0]))
            if has_header
            else tuple(f"Column {index + 1}" for index in range(len(anchor.rows[0].cells)))
        )
        content_column = _content_column(data_rows)
        identity_columns = tuple(index for index in range(len(column_names)) if index != content_column)
        scope = _scope_context(
            raw_blocks,
            anchor_block_index=alignment.region.source_block_index,
            anchor_offset=alignment.region.raw_start,
            fallback_page=anchor.page_number,
        )
        sections = list(scope.section_path)
        table_title = scope.table_title
        scope_fragments = [fragment.model_copy(deep=True) for fragment in scope.source_fragments]
        caption_key = (
            (
                scope.title_fragment.source_block_index,
                scope.title_fragment.char_start,
                scope.title_fragment.char_end,
            )
            if scope.title_fragment is not None
            else None
        )
        if caption_key is not None and caption_key in claimed_captions:
            table_title = None
            scope_fragments = [
                fragment
                for fragment in scope_fragments
                if (
                    fragment.source_block_index,
                    fragment.char_start,
                    fragment.char_end,
                )
                != caption_key
            ]
        table_hash = hashlib.sha256(
            (
                f"{document.id}\x1e{anchor.page_number}\x1e"
                f"{alignment.region.source_block_index}\x1e{alignment.region.raw_start}\x1e"
                f"{anchor.bbox}\x1e{column_names}"
            ).encode()
        ).hexdigest()[:20]
        table_id = f"table-{table_hash}"

        rows = [
            _row_from_evidence(
                table_id=table_id,
                algorithm_version=config.algorithm_version,
                table_title=table_title,
                section_path=sections,
                scope_fragments=scope_fragments,
                column_names=column_names,
                evidence=row,
                aligned=aligned,
                page_number=anchor.page_number,
                insertion_block_index=alignment.region.source_block_index,
                insertion_offset=alignment.region.raw_start,
                row_index=row_index,
            )
            for row_index, (row, aligned) in enumerate(
                zip(data_rows, aligned_data, strict=True),
                start=1,
            )
        ]
        if not rows:
            return None
        if not _apply_merged_cell_inheritance(
            rows,
            data_rows,
            evidence_row_offset=header_rows,
        ):
            return None

        legends: list[_LegendInsertion] = []
        if has_header:
            legend_entries = _legend_entries(anchor.rows[0], alignment.rows[0])
            if legend_entries:
                legends.append(
                    _LegendInsertion(
                        legend=TableLegendData(
                            table_id=table_id,
                            algorithm_version=config.algorithm_version,
                            table_title=table_title,
                            section_path=sections,
                            scope_fragments=[fragment.model_copy(deep=True) for fragment in scope_fragments],
                            entries=legend_entries,
                            page_number=anchor.page_number,
                        ),
                        insertion_block_index=alignment.region.source_block_index,
                        insertion_offset=alignment.region.raw_start,
                    )
                )

        consumed = [alignment.region]
        decisions: list[PageBoundaryDecision] = []
        used_tables = {(anchor.page_number, anchor_index)}
        fallback_reasons: list[str] = []
        open_row = rows[-1]
        current_page = anchor.page_number
        reaches_bottom = anchor.bbox[3] >= 0.82
        previous_bottom = anchor.bbox[3]

        while reaches_bottom:
            next_page_number = current_page + 1
            next_page = pages.get(next_page_number)
            if next_page is None and next_page_number <= page_count:
                additional = await self._evidence_provider.collect(document, {next_page_number})
                pages.update((page.page_number, page) for page in additional)
                next_page = pages.get(next_page_number)
            if next_page is None:
                break
            current_evidence = pages.get(current_page)
            if current_evidence is None or not _compatible_pages(current_evidence, next_page):
                fallback_reasons.append(f"preserved incompatible page boundary {current_page}->{next_page_number}")
                break

            if _page_starts_with_heading(raw_blocks, next_page_number):
                fallback_reasons.append(f"preserved section boundary on page {next_page_number}")
                break

            compatible = self._compatible_top_table(anchor, next_page)
            if compatible is not None:
                if _has_parser_content_before_table(raw_blocks, next_page_number):
                    fallback_reasons.append(f"preserved content before a table on page {next_page_number}")
                    break
                next_index, next_table, geometry_confidence = compatible
                repeated_header = bool(next_table.rows) and _is_repeated_header(
                    next_table.rows[0],
                    column_names,
                )
                data_start = 1 if repeated_header else 0
                if len(next_table.rows) <= data_start:
                    fallback_reasons.append(f"preserved a repeated header without data on page {next_page_number}")
                    break
                if not _spans_are_unambiguous(next_table, header_rows=data_start):
                    fallback_reasons.append(f"preserved ambiguous merged cells on page {next_page_number}")
                    return None
                leading = next_table.rows[data_start]
                identity_empty = all(not leading.cells[index].text.strip() for index in identity_columns)
                content_present = any(
                    cell.text.strip() for cell in leading.cells if cell.column_index not in identity_columns
                )
                bottom_confidence, top_confidence = _page_edge_confidences(
                    previous_bottom,
                    next_table.bbox[1],
                )
                same_table_confidence = min(
                    geometry_confidence,
                    bottom_confidence,
                    top_confidence,
                )
                row_confidence = top_confidence if identity_empty and content_present else 0.0
                if not identity_empty or not content_present:
                    decisions.append(
                        PageBoundaryDecision(
                            previous_page=current_page,
                            next_page=next_page_number,
                            same_table_confidence=same_table_confidence,
                            row_continuation_confidence=row_confidence,
                            decision="preserved",
                            reason="the leading row does not safely continue the open row",
                        )
                    )
                    fallback_reasons.append(f"preserved distinct table boundary {current_page}->{next_page_number}")
                    break

                next_alignment = _align_table(
                    next_table,
                    processed_document,
                    image_regions,
                    self._evidence_provider.provider_id,
                    config.cell_assignment_min_confidence,
                )
                if next_alignment is None:
                    fallback_reasons.append(f"preserved an unaligned table on page {next_page_number}")
                    return None
                if not _all_assignments_pass(
                    leading,
                    next_alignment.rows[data_start],
                    config.cell_assignment_min_confidence,
                ):
                    fallback_reasons.append(f"could not align table cells on page {next_page_number}")
                    return None
                if _has_content_before_regions(
                    raw_blocks,
                    [next_alignment.region],
                ):
                    fallback_reasons.append(f"preserved unaccounted content before a table on page {next_page_number}")
                    break
                aligned_leading = next_alignment.rows[data_start]
                assignment_confidence = min(
                    (
                        confidence
                        for cell, (_, confidence) in zip(
                            leading.cells,
                            aligned_leading,
                            strict=True,
                        )
                        if cell.text.strip()
                    ),
                    default=0.0,
                )
                row_confidence = min(row_confidence, assignment_confidence)
                if (
                    same_table_confidence < config.same_table_min_confidence
                    or row_confidence < config.row_continuation_min_confidence
                ):
                    return None

                decision = PageBoundaryDecision(
                    previous_page=current_page,
                    next_page=next_page_number,
                    same_table_confidence=same_table_confidence,
                    row_continuation_confidence=row_confidence,
                    decision="merged",
                    reason="compatible columns and empty identity cells continue the open row",
                )
                open_row.merge(leading, aligned_leading, decision)
                decisions.append(decision)
                consumed.append(next_alignment.region)
                used_tables.add((next_page_number, next_index))

                next_rows: list[_MutableRow] = []
                following_start = data_start + 1
                for evidence_index, row in enumerate(
                    next_table.rows[following_start:],
                    start=following_start,
                ):
                    aligned = next_alignment.rows[evidence_index]
                    if not _all_assignments_pass(row, aligned, config.cell_assignment_min_confidence):
                        fallback_reasons.append(f"preserved an unaligned row on page {next_page_number}")
                        return None
                    new_row = _row_from_evidence(
                        table_id=table_id,
                        algorithm_version=config.algorithm_version,
                        table_title=table_title,
                        section_path=sections,
                        scope_fragments=[fragment.model_copy(deep=True) for fragment in scope_fragments],
                        column_names=column_names,
                        evidence=row,
                        aligned=aligned,
                        page_number=next_page_number,
                        insertion_block_index=next_alignment.region.source_block_index,
                        insertion_offset=next_alignment.region.raw_start,
                        row_index=len(rows) + len(next_rows) + 1,
                    )
                    next_rows.append(new_row)
                if not _apply_merged_cell_inheritance(
                    next_rows,
                    next_table.rows[following_start:],
                    evidence_row_offset=following_start,
                ):
                    fallback_reasons.append(f"preserved ambiguous merged-cell inheritance on page {next_page_number}")
                    return None
                rows.extend(next_rows)
                if next_rows:
                    open_row = next_rows[-1]

                current_page = next_page_number
                reaches_bottom = next_table.bbox[3] >= 0.82
                previous_bottom = next_table.bbox[3]
                continue

            sparse = _sparse_continuation(next_page, anchor.column_bounds, identity_columns)
            if sparse is None:
                break
            aligned_sparse = _align_sparse_row(
                sparse,
                next_page_number,
                processed_document,
                image_regions,
                self._evidence_provider.provider_id,
                config.cell_assignment_min_confidence,
            )
            if aligned_sparse is None or not _all_assignments_pass(
                sparse,
                aligned_sparse.cells,
                config.cell_assignment_min_confidence,
            ):
                fallback_reasons.append(f"could not align sparse continuation on page {next_page_number}")
                return None
            if _has_content_before_regions(raw_blocks, aligned_sparse.regions):
                fallback_reasons.append(
                    f"preserved unaccounted content before a continuation on page {next_page_number}"
                )
                break

            bottom_confidence, top_confidence = _page_edge_confidences(
                previous_bottom,
                sparse.bbox[1],
            )
            assignment_confidence = min(
                (
                    confidence
                    for cell, (_, confidence) in zip(
                        sparse.cells,
                        aligned_sparse.cells,
                        strict=True,
                    )
                    if cell.text.strip()
                ),
                default=0.0,
            )
            same_table_confidence = min(bottom_confidence, top_confidence)
            row_confidence = min(top_confidence, assignment_confidence)
            if (
                same_table_confidence < config.same_table_min_confidence
                or row_confidence < config.row_continuation_min_confidence
            ):
                return None
            decision = PageBoundaryDecision(
                previous_page=current_page,
                next_page=next_page_number,
                same_table_confidence=same_table_confidence,
                row_continuation_confidence=row_confidence,
                decision="merged",
                reason="content remains inside continuation columns at the next page boundary",
            )
            open_row.merge(sparse, aligned_sparse.cells, decision)
            decisions.append(decision)
            consumed.extend(aligned_sparse.regions)
            current_page = next_page_number
            reaches_bottom = sparse.bbox[3] >= 0.82
            previous_bottom = sparse.bbox[3]

        chain = _NormalizedChain(
            rows=rows,
            identity_columns=identity_columns,
            consumed_regions=consumed,
            decisions=decisions,
            used_tables=used_tables,
            fallback_reasons=fallback_reasons,
            legends=legends,
        )
        if caption_key is not None:
            claimed_captions.add(caption_key)
        return chain

    @staticmethod
    def _compatible_top_table(
        anchor: LayoutTableEvidence,
        page: PageLayoutEvidence,
    ) -> tuple[int, LayoutTableEvidence, float] | None:
        for table_index, table in sorted(enumerate(page.tables), key=lambda item: item[1].bbox[1]):
            if table.bbox[1] > 0.15:
                continue
            confidence = _table_geometry_confidence(anchor.column_bounds, table.column_bounds)
            if confidence > 0.0:
                return table_index, table, confidence
        return None

    @staticmethod
    def _unchanged(
        processed_document: ProcessedDocument,
        config: TableReconstructionConfig,
        reason: str,
    ) -> ProcessedDocument:
        return processed_document.model_copy(
            update={
                "normalization_report": NormalizationReport(
                    algorithm_version=config.algorithm_version,
                    status="unchanged",
                    fallback_reasons=[reason],
                )
            }
        )


def _row_text(row: TableRowData) -> str:
    return render_table_row(row)


def _legend_text(legend: TableLegendData) -> str:
    return render_table_legend(legend)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _regions_are_disjoint(regions: list[_AlignedRegion]) -> bool:
    by_block: dict[int, list[_AlignedRegion]] = {}
    for region in regions:
        by_block.setdefault(region.source_block_index, []).append(region)
    for block_regions in by_block.values():
        ordered = sorted(block_regions, key=lambda region: (region.working_start, region.working_end))
        if any(current.working_start < previous.working_end for previous, current in zip(ordered, ordered[1:])):
            return False
    return True


def _map_interval(raw: str, working: str, start: int, end: int) -> tuple[int, int] | None:
    if raw == working:
        return start, end
    found = _find_normalised_range(raw[start:end], working)
    if found is None:
        return None
    mapped_start, mapped_end, _ = found
    return mapped_start, mapped_end


def _build_normalized_blocks(
    processed_document: ProcessedDocument,
    rows: list[tuple[_MutableRow, tuple[int, ...]]],
    legends: list[_LegendInsertion],
    consumed: list[_AlignedRegion],
) -> list[TextBlock] | None:
    raw_blocks = processed_document.raw_text_blocks
    if raw_blocks is None:
        return None

    consumed_by_block: dict[int, list[_AlignedRegion]] = {}
    for region in consumed:
        consumed_by_block.setdefault(region.source_block_index, []).append(region)
    rows_by_block: dict[int, list[tuple[_MutableRow, tuple[int, ...]]]] = {}
    for row in rows:
        rows_by_block.setdefault(row[0].insertion_block_index, []).append(row)
    legends_by_block: dict[int, list[_LegendInsertion]] = {}
    for legend in legends:
        legends_by_block.setdefault(legend.insertion_block_index, []).append(legend)

    normalized: list[TextBlock] = []
    for block_index, working_block in enumerate(processed_document.text_blocks):
        if block_index >= len(raw_blocks):
            normalized.append(working_block)
            continue
        raw_block = raw_blocks[block_index]
        regions = consumed_by_block.get(block_index, [])
        if not regions:
            normalized.append(working_block)
            continue

        mapped_regions: list[tuple[int, int, int, int]] = []
        for region in regions:
            if (
                region.raw_end > len(raw_block.text)
                or region.working_end > len(working_block.text)
                or region.raw_start >= region.raw_end
                or region.working_start >= region.working_end
            ):
                return None
            mapped_regions.append(
                (
                    region.raw_start,
                    region.raw_end,
                    region.working_start,
                    region.working_end,
                )
            )
        merged_working = _merge_intervals([(start, end) for _, _, start, end in mapped_regions])

        insertions = sorted(rows_by_block.get(block_index, []), key=lambda item: item[0].insertion_offset)
        legend_insertions = sorted(
            legends_by_block.get(block_index, []),
            key=lambda item: item.insertion_offset,
        )
        inserted: set[int] = set()
        inserted_legends: set[int] = set()
        cursor = 0
        for working_start, working_end in merged_working:
            residual = working_block.text[cursor:working_start].strip()
            if residual:
                normalized.append(
                    working_block.model_copy(
                        update={
                            "text": residual,
                            "source_fragments": [],
                            "table_row": None,
                            "table_legend": None,
                        }
                    )
                )
            raw_ranges = [
                (raw_start, raw_end)
                for raw_start, raw_end, mapped_start, mapped_end in mapped_regions
                if mapped_start <= working_end and mapped_end >= working_start
            ]
            raw_limit = max((end for _, end in raw_ranges), default=len(raw_block.text))
            for insertion_index, insertion in enumerate(legend_insertions):
                if insertion_index in inserted_legends or insertion.insertion_offset > raw_limit:
                    continue
                legend = insertion.legend
                fragments = [
                    *legend.scope_fragments,
                    *(fragment for entry in legend.entries for fragment in entry.source_fragments),
                ]
                normalized.append(
                    TextBlock(
                        text=_legend_text(legend),
                        page_number=legend.page_number,
                        block_type="table_legend",
                        metadata={
                            "table_id": legend.table_id,
                            "table_content_kind": "legend",
                        },
                        source_fragments=fragments,
                        table_legend=legend,
                    )
                )
                inserted_legends.add(insertion_index)
            for insertion_index, (mutable_row, identity_columns) in enumerate(insertions):
                if insertion_index in inserted or mutable_row.insertion_offset > raw_limit:
                    continue
                row = mutable_row.freeze(identity_columns)
                fragments = [
                    *row.scope_fragments,
                    *(fragment for cell in row.cells for fragment in cell.source_fragments),
                ]
                normalized.append(
                    TextBlock(
                        text=_row_text(row),
                        page_number=row.page_start,
                        block_type="table_row",
                        metadata={
                            "table_id": row.table_id,
                            "row_id": row.row_id,
                            "row_index": row.row_index,
                            "page_end": row.page_end,
                        },
                        source_fragments=fragments,
                        table_row=row,
                    )
                )
                inserted.add(insertion_index)
            cursor = working_end

        residual = working_block.text[cursor:].strip()
        if residual:
            normalized.append(
                working_block.model_copy(
                    update={
                        "text": residual,
                        "source_fragments": [],
                        "table_row": None,
                        "table_legend": None,
                    }
                )
            )
        for insertion_index, insertion in enumerate(legend_insertions):
            if insertion_index in inserted_legends:
                continue
            legend = insertion.legend
            fragments = [
                *legend.scope_fragments,
                *(fragment for entry in legend.entries for fragment in entry.source_fragments),
            ]
            normalized.append(
                TextBlock(
                    text=_legend_text(legend),
                    page_number=legend.page_number,
                    block_type="table_legend",
                    metadata={
                        "table_id": legend.table_id,
                        "table_content_kind": "legend",
                    },
                    source_fragments=fragments,
                    table_legend=legend,
                )
            )
        for insertion_index, (mutable_row, identity_columns) in enumerate(insertions):
            if insertion_index in inserted:
                continue
            row = mutable_row.freeze(identity_columns)
            fragments = [
                *row.scope_fragments,
                *(fragment for cell in row.cells for fragment in cell.source_fragments),
            ]
            normalized.append(
                TextBlock(
                    text=_row_text(row),
                    page_number=row.page_start,
                    block_type="table_row",
                    metadata={
                        "table_id": row.table_id,
                        "row_id": row.row_id,
                        "row_index": row.row_index,
                        "page_end": row.page_end,
                    },
                    source_fragments=fragments,
                    table_row=row,
                )
            )

    return normalized


__all__ = ["DeterministicTableNormalizer"]
