"""PyMuPDF adapter for table and word-position evidence."""

from __future__ import annotations

import unicodedata
from statistics import median
from typing import Any

import pymupdf
from core.indexing.parsers.pdf.pymupdf_runtime import run_pymupdf
from core.indexing.structure_normalizer import (
    LayoutCellEvidence,
    LayoutRowEvidence,
    LayoutTableEvidence,
    LayoutWord,
    NormalizedBBox,
    PageLayoutEvidence,
    TableLayoutEvidenceProvider,
)
from core.models.document import Document, DocumentType


def _normalize_bbox(bbox: tuple[float, float, float, float], width: float, height: float) -> NormalizedBBox:
    x0, y0, x1, y1 = bbox
    return (x0 / width, y0 / height, x1 / width, y1 / height)


def _row_bbox(cells: list[tuple[float, float, float, float] | None]) -> tuple[float, float, float, float] | None:
    present = [cell for cell in cells if cell is not None]
    if not present:
        return None
    return (
        min(cell[0] for cell in present),
        min(cell[1] for cell in present),
        max(cell[2] for cell in present),
        max(cell[3] for cell in present),
    )


def _column_bounds(
    rows: list[Any],
    table_bbox: tuple[float, float, float, float],
    column_count: int,
    width: float,
) -> tuple[tuple[float, float], ...]:
    for row in rows:
        if len(row.cells) != column_count or any(cell is None for cell in row.cells):
            continue
        return tuple((cell[0] / width, cell[2] / width) for cell in row.cells)

    x0, _, x1, _ = table_bbox
    column_width = (x1 - x0) / max(column_count, 1)
    return tuple(
        ((x0 + index * column_width) / width, (x0 + (index + 1) * column_width) / width)
        for index in range(column_count)
    )


def _normalized_alnum(text: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKC", text).casefold() if character.isalnum())


def _word_is_inside_cell(word: LayoutWord, bbox: NormalizedBBox) -> bool:
    x0, y0, x1, y1 = word.bbox
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2
    cell_x0, cell_y0, cell_x1, cell_y1 = bbox
    return cell_x0 <= center_x <= cell_x1 and cell_y0 <= center_y <= cell_y1


def _text_from_positioned_words(
    words: tuple[LayoutWord, ...],
    bbox: NormalizedBBox | None,
    extracted_text: str,
) -> str:
    """Rebuild cell text from PDF words when they match the extracted value.

    ``Table.extract()`` occasionally inserts spaces inside words or removes
    spaces between adjacent words. PyMuPDF's positioned words retain the
    intended token boundaries. The alphanumeric equality gate ensures geometry
    is used only when it accounts for the complete extracted value; otherwise
    the parser value remains the safe fallback.
    """

    if bbox is None:
        return extracted_text

    cell_words = sorted(
        (word for word in words if word.text.strip() and _word_is_inside_cell(word, bbox)),
        key=lambda word: (
            word.block_number,
            word.line_number,
            word.word_number,
            word.bbox[1],
            word.bbox[0],
        ),
    )
    if not cell_words:
        return extracted_text

    parts: list[str] = []
    current_key: tuple[int, int] | None = None
    current_words: list[str] = []
    previous_key: tuple[int, int] | None = None

    def flush_line() -> None:
        nonlocal previous_key
        if current_key is None or not current_words:
            return
        if previous_key is not None:
            previous_block, previous_line = previous_key
            current_block, current_line = current_key
            parts.append("\n\n" if current_block != previous_block or current_line > previous_line + 1 else "\n")
        parts.append(" ".join(current_words))
        previous_key = current_key

    for word in cell_words:
        key = (word.block_number, word.line_number)
        if key != current_key:
            flush_line()
            current_key = key
            current_words = []
        current_words.append(word.text.strip())
    flush_line()

    rebuilt = "".join(parts).strip()
    if not rebuilt:
        return extracted_text
    if extracted_text.strip() and _normalized_alnum(rebuilt) != _normalized_alnum(extracted_text):
        return extracted_text
    return rebuilt


def _cell_grid_semantics(
    table_rows: list[Any],
    column_bounds: tuple[tuple[float, float], ...],
    table_bbox: tuple[float, float, float, float],
    width: float,
    height: float,
) -> dict[tuple[int, int], tuple[str, int, int, tuple[int, int] | None]]:
    """Resolve explicit empty and merged slots from PyMuPDF's cell geometry."""
    table_top = table_bbox[1]
    table_bottom = table_bbox[3]
    equal_height = (table_bottom - table_top) / max(len(table_rows), 1)
    row_tops: list[float | None] = []
    for row in table_rows:
        cell_tops = [cell[1] for cell in row.cells if cell is not None]
        row_tops.append(float(median(cell_tops)) if cell_tops else None)

    known = [(index, top) for index, top in enumerate(row_tops) if top is not None]
    for index, top in enumerate(row_tops):
        if top is not None:
            continue
        previous = next(
            ((candidate_index, candidate) for candidate_index, candidate in reversed(known) if candidate_index < index),
            None,
        )
        following = next(
            ((candidate_index, candidate) for candidate_index, candidate in known if candidate_index > index),
            None,
        )
        if previous is not None and following is not None:
            left_index, left = previous
            right_index, right = following
            ratio = (index - left_index) / (right_index - left_index)
            row_tops[index] = left + (right - left) * ratio
        else:
            row_tops[index] = table_top + index * equal_height

    resolved_tops = [float(top) for top in row_tops]
    if any(right <= left for left, right in zip(resolved_tops, resolved_tops[1:], strict=False)):
        resolved_tops = [table_top + index * equal_height for index in range(len(table_rows))]
    row_centers = [
        (top + (resolved_tops[index + 1] if index + 1 < len(resolved_tops) else table_bottom)) / (2 * height)
        for index, top in enumerate(resolved_tops)
    ]
    anchors: dict[tuple[int, int], tuple[int, int]] = {}
    coverage: dict[tuple[int, int], list[tuple[int, int]]] = {}

    for row_index, row in enumerate(table_rows):
        for column_index, raw_bbox in enumerate(row.cells):
            if raw_bbox is None:
                continue
            bbox = _normalize_bbox(raw_bbox, width, height)
            covered_columns = [
                index for index, (left, right) in enumerate(column_bounds) if bbox[0] <= (left + right) / 2 <= bbox[2]
            ]
            covered_rows = [index for index, center in enumerate(row_centers) if bbox[1] <= center <= bbox[3]]
            if not covered_columns:
                covered_columns = [column_index]
            if not covered_rows:
                covered_rows = [row_index]
            column_span = max(covered_columns) - min(covered_columns) + 1
            row_span = max(covered_rows) - min(covered_rows) + 1
            anchors[(row_index, column_index)] = (column_span, row_span)
            for covered_row in covered_rows:
                for covered_column in covered_columns:
                    if (covered_row, covered_column) != (row_index, column_index):
                        coverage.setdefault((covered_row, covered_column), []).append((row_index, column_index))

    semantics: dict[tuple[int, int], tuple[str, int, int, tuple[int, int] | None]] = {}
    for row_index, row in enumerate(table_rows):
        for column_index, raw_bbox in enumerate(row.cells):
            key = (row_index, column_index)
            if raw_bbox is not None:
                column_span, row_span = anchors[key]
                semantics[key] = ("value", column_span, row_span, None)
                continue
            covering = coverage.get(key, [])
            if len(covering) == 1:
                semantics[key] = ("covered", 1, 1, covering[0])
            else:
                semantics[key] = ("unknown", 1, 1, None)
    return semantics


def _collect_evidence(raw_bytes: bytes, page_numbers: tuple[int, ...]) -> list[PageLayoutEvidence]:
    collected: list[PageLayoutEvidence] = []
    with pymupdf.open(stream=raw_bytes, filetype="pdf") as pdf:
        for page_number in page_numbers:
            if page_number < 1 or page_number > pdf.page_count:
                continue

            page = pdf[page_number - 1]
            width = float(page.rect.width)
            height = float(page.rect.height)
            words = tuple(
                LayoutWord(
                    text=str(word[4]),
                    bbox=_normalize_bbox((word[0], word[1], word[2], word[3]), width, height),
                    block_number=int(word[5]),
                    line_number=int(word[6]),
                    word_number=int(word[7]),
                )
                for word in page.get_text("words", sort=True)
            )

            tables: list[LayoutTableEvidence] = []
            for table in page.find_tables().tables:
                extracted = table.extract()
                table_rows = list(table.rows)
                column_bounds = _column_bounds(
                    table_rows,
                    table.bbox,
                    table.col_count,
                    width,
                )
                grid_semantics = _cell_grid_semantics(
                    table_rows,
                    column_bounds,
                    table.bbox,
                    width,
                    height,
                )
                rows: list[LayoutRowEvidence] = []
                for row_index, row in enumerate(table_rows):
                    bbox = getattr(row, "bbox", None) or _row_bbox(row.cells)
                    if bbox is None:
                        continue
                    values = extracted[row_index] if row_index < len(extracted) else []
                    cells: list[LayoutCellEvidence] = []
                    for column_index, cell_bbox in enumerate(row.cells):
                        normalized_bbox = _normalize_bbox(cell_bbox, width, height) if cell_bbox is not None else None
                        extracted_value = values[column_index] if column_index < len(values) else None
                        extracted_text = str(extracted_value or "")
                        slot_state, column_span, row_span, covered_by = grid_semantics[(row_index, column_index)]
                        if slot_state == "value" and not extracted_text:
                            slot_state = "explicit_empty"
                        cells.append(
                            LayoutCellEvidence(
                                column_index=column_index,
                                text=_text_from_positioned_words(words, normalized_bbox, extracted_text),
                                bbox=normalized_bbox,
                                slot_state=slot_state,
                                column_span=column_span,
                                row_span=row_span,
                                covered_by=covered_by,
                            )
                        )
                    rows.append(
                        LayoutRowEvidence(
                            cells=tuple(cells),
                            bbox=_normalize_bbox(bbox, width, height),
                        )
                    )

                if not rows:
                    continue
                tables.append(
                    LayoutTableEvidence(
                        page_number=page_number,
                        bbox=_normalize_bbox(table.bbox, width, height),
                        column_bounds=column_bounds,
                        rows=tuple(rows),
                    )
                )

            collected.append(
                PageLayoutEvidence(
                    page_number=page_number,
                    width=width,
                    height=height,
                    words=words,
                    tables=tuple(tables),
                )
            )
    return collected


def _discover_table_pages(raw_bytes: bytes) -> set[int]:
    """Find table-bearing pages without extracting their text or geometry."""
    discovered: set[int] = set()
    with pymupdf.open(stream=raw_bytes, filetype="pdf") as pdf:
        for page_number, page in enumerate(pdf, start=1):
            if page.find_tables().tables:
                discovered.add(page_number)
    return discovered


class PyMuPDFTableEvidenceProvider(TableLayoutEvidenceProvider):
    """Collect layout evidence from the original PDF on the shared executor."""

    provider_id = "pymupdf"

    async def discover(self, document: Document) -> set[int]:
        if document.content_type is not DocumentType.PDF or not document.raw_bytes:
            return set()
        return await run_pymupdf(
            _discover_table_pages,
            document.raw_bytes,
        )

    async def collect(self, document: Document, page_numbers: set[int]) -> list[PageLayoutEvidence]:
        if document.content_type is not DocumentType.PDF or not document.raw_bytes or not page_numbers:
            return []
        return await run_pymupdf(
            _collect_evidence,
            document.raw_bytes,
            tuple(sorted(page_numbers)),
        )


__all__ = ["PyMuPDFTableEvidenceProvider"]
