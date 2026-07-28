"""PyMuPDF adapter for table and word-position evidence."""

from __future__ import annotations

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
                rows: list[LayoutRowEvidence] = []
                for row_index, row in enumerate(table.rows):
                    bbox = _row_bbox(row.cells)
                    if bbox is None:
                        continue
                    values = extracted[row_index] if row_index < len(extracted) else []
                    cells = tuple(
                        LayoutCellEvidence(
                            column_index=column_index,
                            text=str(values[column_index] or "") if column_index < len(values) else "",
                            bbox=(_normalize_bbox(cell_bbox, width, height) if cell_bbox is not None else None),
                        )
                        for column_index, cell_bbox in enumerate(row.cells)
                    )
                    rows.append(
                        LayoutRowEvidence(
                            cells=cells,
                            bbox=_normalize_bbox(bbox, width, height),
                        )
                    )

                if not rows:
                    continue
                tables.append(
                    LayoutTableEvidence(
                        page_number=page_number,
                        bbox=_normalize_bbox(table.bbox, width, height),
                        column_bounds=_column_bounds(
                            list(table.rows),
                            table.bbox,
                            table.col_count,
                            width,
                        ),
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


class PyMuPDFTableEvidenceProvider(TableLayoutEvidenceProvider):
    """Collect layout evidence from the original PDF on the shared executor."""

    provider_id = "pymupdf"

    async def collect(self, document: Document, page_numbers: set[int]) -> list[PageLayoutEvidence]:
        if document.content_type is not DocumentType.PDF or not document.raw_bytes or not page_numbers:
            return []
        return await run_pymupdf(
            _collect_evidence,
            document.raw_bytes,
            tuple(sorted(page_numbers)),
        )


__all__ = ["PyMuPDFTableEvidenceProvider"]
