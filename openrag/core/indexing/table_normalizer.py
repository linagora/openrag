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
from core.models.document import (
    Document,
    DocumentType,
    NormalizationReport,
    PageBoundaryDecision,
    ProcessedDocument,
    SourceFragment,
    TableCellData,
    TableRowData,
    TextBlock,
)
from core.prompts.vlm_prompt_builder import wrap_caption

_MARKDOWN_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.+?)\s*$")
_SYNTHETIC_COLUMN_RE = re.compile(r"^col(?:umn)?\s*\d+$", re.IGNORECASE)
_HEADER_TERMS = {
    "category",
    "catégorie",
    "description",
    "document",
    "documents",
    "intitulé",
    "libellé",
    "name",
    "pièce",
    "pièces",
    "reference",
    "référence",
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
        )


@dataclass(slots=True)
class _MutableRow:
    table_id: str
    algorithm_version: str
    table_title: str | None
    section_path: list[str]
    cells: list[_MutableCell]
    page_start: int
    page_end: int
    insertion_block_index: int
    insertion_offset: int
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
        row_hash = hashlib.sha256(f"{self.table_id}\x1e{identity}\x1e{self.page_start}".encode()).hexdigest()[:20]
        return TableRowData(
            table_id=self.table_id,
            row_id=f"row-{row_hash}",
            algorithm_version=self.algorithm_version,
            table_title=self.table_title,
            section_path=self.section_path,
            cells=[cell.freeze() for cell in self.cells],
            identity_columns=list(identity_columns),
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

    confidence = _similarity(evidence_cell.text, markdown_cell.text)
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


def _align_table(
    table: LayoutTableEvidence,
    processed_document: ProcessedDocument,
    image_regions: dict[int, list[tuple[_AlignedRegion, str]]],
    evidence_provider: str,
    threshold: float,
) -> _TableAlignment | None:
    raw_blocks = processed_document.raw_text_blocks or []
    candidates: list[tuple[float, int, _MarkdownTable, list[_MarkdownRow]]] = []
    for block_index, block in enumerate(raw_blocks):
        if block.page_number != table.page_number:
            continue
        for markdown_table in _markdown_tables(block.text):
            content_rows = [row for row in markdown_table.rows if not row.separator]
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
                    score = sum(cell_scores) / len(cell_scores) if cell_scores else 0.0
                    if score > best_score:
                        best_score = score
                        best_index = row_index
                if best_index is None:
                    break
                matched.append(content_rows[best_index])
                scores.append(best_score)
                cursor = best_index + 1
            if len(matched) == len(table.rows):
                candidates.append((sum(scores) / len(scores), block_index, markdown_table, matched))

    if not candidates:
        table_text = "\n".join(cell.text for row in table.rows for cell in row.cells if cell.text.strip())
        image_region = _match_image_region(table_text, table.page_number, image_regions, threshold)
        if image_region is None:
            return None
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

    score, block_index, markdown_table, matched_rows = max(candidates, key=lambda candidate: candidate[0])
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
                aligned_cells.append((None, 1.0))
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
            aligned_cells.append(
                (
                    SourceFragment(
                        source_block_index=block_index,
                        page_number=table.page_number,
                        char_start=markdown_cell.start,
                        char_end=markdown_cell.end,
                        bbox=evidence_cell.bbox,
                        text_start=0,
                        text_end=max(1, len(_clean_cell_text(evidence_cell.text))),
                    ),
                    confidence,
                )
            )
        aligned_rows.append(aligned_cells)

    column_names: list[str] = []
    for column_index, (evidence_cell, markdown_cell, (fragment, confidence)) in enumerate(
        zip(table.rows[0].cells, matched_rows[0].cells, aligned_rows[0], strict=True)
    ):
        parser_header_is_trusted = (
            fragment is not None
            and fragment.source_kind == "parser_text"
            and confidence >= threshold
            and bool(markdown_cell.text.strip())
        )
        source_text = markdown_cell.text if parser_header_is_trusted else evidence_cell.text
        column_names.append(_concise_column_name(source_text, column_index))

    return _TableAlignment(
        region=table_region,
        rows=aligned_rows,
        column_names=tuple(column_names),
    )


def _looks_like_header(row: LayoutRowEvidence, following: LayoutRowEvidence | None) -> bool:
    nonempty = sum(bool(cell.text.strip()) for cell in row.cells)
    if nonempty < max(2, (len(row.cells) + 1) // 2):
        return False
    text = " ".join(cell.text for cell in row.cells).casefold()
    if any(term in text for term in _HEADER_TERMS):
        return True
    if following is None or not row.cells or not following.cells:
        return False
    first = row.cells[0].text.strip()
    next_first = following.cells[0].text.strip()
    return not first and bool(next_first)


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


def _section_path(text: str, before: int) -> list[str]:
    return [_clean_cell_text(match.group(1)).strip("* ") for match in _HEADING_RE.finditer(text[:before])]


def _row_from_evidence(
    *,
    table_id: str,
    algorithm_version: str,
    table_title: str | None,
    section_path: list[str],
    column_names: tuple[str, ...],
    evidence: LayoutRowEvidence,
    aligned: list[tuple[SourceFragment | None, float]],
    page_number: int,
    insertion_block_index: int,
    insertion_offset: int,
) -> _MutableRow:
    cells: list[_MutableCell] = []
    for name, cell_evidence, (fragment, confidence) in zip(column_names, evidence.cells, aligned, strict=True):
        cell = _MutableCell(column_index=cell_evidence.column_index, column_name=name)
        cell.append(cell_evidence.text, [fragment] if fragment is not None else [], confidence)
        cells.append(cell)
    return _MutableRow(
        table_id=table_id,
        algorithm_version=algorithm_version,
        table_title=table_title,
        section_path=section_path,
        cells=cells,
        page_start=page_number,
        page_end=page_number,
        insertion_block_index=insertion_block_index,
        insertion_offset=insertion_offset,
    )


def _all_assignments_pass(
    row: LayoutRowEvidence,
    aligned: list[tuple[SourceFragment | None, float]],
    threshold: float,
) -> bool:
    return all(
        not cell.text.strip() or (fragment is not None and confidence >= threshold)
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
                )
                if chain is None:
                    continue
                chains.append(chain)
                used_tables.update(chain.used_tables)

        if not chains:
            return self._unchanged(processed_document, config, "no page boundary passed all confidence gates")

        rows: list[tuple[_MutableRow, tuple[int, ...]]] = []
        consumed: list[_AlignedRegion] = []
        decisions: list[PageBoundaryDecision] = []
        fallback_reasons: list[str] = []
        for chain in chains:
            rows.extend((row, chain.identity_columns) for row in chain.rows)
            consumed.extend(chain.consumed_regions)
            decisions.extend(chain.decisions)
            fallback_reasons.extend(chain.fallback_reasons)

        if not _regions_are_disjoint(consumed):
            return self._unchanged(processed_document, config, "candidate table overlays overlap")

        normalized_blocks = _build_normalized_blocks(
            processed_document,
            rows,
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
    ) -> _NormalizedChain | None:
        raw_blocks = processed_document.raw_text_blocks or []
        if len(anchor.rows) < 2 or anchor.bbox[3] < 0.82:
            return None
        if not _looks_like_header(anchor.rows[0], anchor.rows[1]):
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
        data_rows = anchor.rows[1:]
        aligned_data = alignment.rows[1:]
        if any(
            not _all_assignments_pass(row, aligned, config.cell_assignment_min_confidence)
            for row, aligned in zip(data_rows, aligned_data, strict=True)
        ):
            return None

        column_names = alignment.column_names or _column_names(anchor.rows[0])
        content_column = _content_column(data_rows)
        identity_columns = tuple(index for index in range(len(column_names)) if index != content_column)
        source_block = raw_blocks[alignment.region.source_block_index]
        sections = _section_path(source_block.text, alignment.region.raw_start)
        table_title = sections[-1] if sections else document.filename or None
        table_hash = hashlib.sha256(f"{document.id}\x1e{anchor.page_number}\x1e{column_names}".encode()).hexdigest()[
            :20
        ]
        table_id = f"table-{table_hash}"

        rows = [
            _row_from_evidence(
                table_id=table_id,
                algorithm_version=config.algorithm_version,
                table_title=table_title,
                section_path=sections,
                column_names=column_names,
                evidence=row,
                aligned=aligned,
                page_number=anchor.page_number,
                insertion_block_index=alignment.region.source_block_index,
                insertion_offset=alignment.region.raw_start,
            )
            for row, aligned in zip(data_rows, aligned_data, strict=True)
        ]
        if not rows:
            return None

        consumed = [alignment.region]
        decisions: list[PageBoundaryDecision] = []
        used_tables = {(anchor.page_number, anchor_index)}
        fallback_reasons: list[str] = []
        open_row = rows[-1]
        current_page = anchor.page_number
        reaches_bottom = anchor.bbox[3] >= 0.82
        merged_boundaries = 0

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

            compatible = self._compatible_top_table(anchor, next_page)
            if compatible is not None:
                next_index, next_table, same_table_confidence = compatible
                leading = next_table.rows[0]
                identity_empty = all(not leading.cells[index].text.strip() for index in identity_columns)
                content_present = any(
                    cell.text.strip() for cell in leading.cells if cell.column_index not in identity_columns
                )
                row_confidence = 0.99 if identity_empty and content_present else 0.0
                if (
                    same_table_confidence < config.same_table_min_confidence
                    or row_confidence < config.row_continuation_min_confidence
                ):
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
                    fallback_reasons.append(f"preserved boundary {current_page}->{next_page_number}")
                    break

                next_alignment = _align_table(
                    next_table,
                    processed_document,
                    image_regions,
                    self._evidence_provider.provider_id,
                    config.cell_assignment_min_confidence,
                )
                if next_alignment is None or not _all_assignments_pass(
                    leading,
                    next_alignment.rows[0] if next_alignment else [],
                    config.cell_assignment_min_confidence,
                ):
                    fallback_reasons.append(f"could not align table cells on page {next_page_number}")
                    break

                decision = PageBoundaryDecision(
                    previous_page=current_page,
                    next_page=next_page_number,
                    same_table_confidence=same_table_confidence,
                    row_continuation_confidence=row_confidence,
                    decision="merged",
                    reason="compatible columns and empty identity cells continue the open row",
                )
                open_row.merge(leading, next_alignment.rows[0], decision)
                decisions.append(decision)
                merged_boundaries += 1
                consumed.append(next_alignment.region)
                used_tables.add((next_page_number, next_index))

                for row_index, row in enumerate(next_table.rows[1:], start=1):
                    aligned = next_alignment.rows[row_index]
                    if not _all_assignments_pass(row, aligned, config.cell_assignment_min_confidence):
                        fallback_reasons.append(f"preserved an unaligned row on page {next_page_number}")
                        return None
                    new_row = _row_from_evidence(
                        table_id=table_id,
                        algorithm_version=config.algorithm_version,
                        table_title=table_title,
                        section_path=sections,
                        column_names=column_names,
                        evidence=row,
                        aligned=aligned,
                        page_number=next_page_number,
                        insertion_block_index=next_alignment.region.source_block_index,
                        insertion_offset=next_alignment.region.raw_start,
                    )
                    rows.append(new_row)
                    open_row = new_row

                current_page = next_page_number
                reaches_bottom = next_table.bbox[3] >= 0.82
                continue

            if _page_starts_with_heading(raw_blocks, next_page_number):
                fallback_reasons.append(f"preserved section boundary on page {next_page_number}")
                break
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
                break

            same_table_confidence = 0.97
            row_confidence = 0.98
            if (
                same_table_confidence < config.same_table_min_confidence
                or row_confidence < config.row_continuation_min_confidence
            ):
                break
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
            merged_boundaries += 1
            consumed.extend(aligned_sparse.regions)
            current_page = next_page_number
            reaches_bottom = sparse.bbox[3] >= 0.82

        if merged_boundaries == 0:
            return None
        return _NormalizedChain(
            rows=rows,
            identity_columns=identity_columns,
            consumed_regions=consumed,
            decisions=decisions,
            used_tables=used_tables,
            fallback_reasons=fallback_reasons,
        )

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
    lines: list[str] = []
    if row.section_path:
        lines.append(f"Section: {' > '.join(row.section_path)}")
    if row.table_title:
        lines.append(f"Table: {row.table_title}")
    lines.extend(f"{cell.column_name or f'Column {cell.column_index + 1}'}: {cell.text}" for cell in row.cells)
    return "\n".join(lines)


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
        inserted: set[int] = set()
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
                        }
                    )
                )
            raw_ranges = [
                (raw_start, raw_end)
                for raw_start, raw_end, mapped_start, mapped_end in mapped_regions
                if mapped_start <= working_end and mapped_end >= working_start
            ]
            raw_limit = max((end for _, end in raw_ranges), default=len(raw_block.text))
            for insertion_index, (mutable_row, identity_columns) in enumerate(insertions):
                if insertion_index in inserted or mutable_row.insertion_offset > raw_limit:
                    continue
                row = mutable_row.freeze(identity_columns)
                fragments = [fragment for cell in row.cells for fragment in cell.source_fragments]
                normalized.append(
                    TextBlock(
                        text=_row_text(row),
                        page_number=row.page_start,
                        block_type="table_row",
                        metadata={
                            "table_id": row.table_id,
                            "row_id": row.row_id,
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
                    }
                )
            )
        for insertion_index, (mutable_row, identity_columns) in enumerate(insertions):
            if insertion_index in inserted:
                continue
            row = mutable_row.freeze(identity_columns)
            fragments = [fragment for cell in row.cells for fragment in cell.source_fragments]
            normalized.append(
                TextBlock(
                    text=_row_text(row),
                    page_number=row.page_start,
                    block_type="table_row",
                    metadata={"table_id": row.table_id, "row_id": row.row_id, "page_end": row.page_end},
                    source_fragments=fragments,
                    table_row=row,
                )
            )

    return normalized


__all__ = ["DeterministicTableNormalizer"]
