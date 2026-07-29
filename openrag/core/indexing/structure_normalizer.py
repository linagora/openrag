"""Contracts and evidence models for parser-independent structure normalization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from core.config.table_reconstruction import TableReconstructionConfig
from core.models.document import Document, ProcessedDocument

NormalizedBBox = tuple[float, float, float, float]


@dataclass(slots=True, frozen=True)
class LayoutWord:
    text: str
    bbox: NormalizedBBox
    block_number: int
    line_number: int
    word_number: int


@dataclass(slots=True, frozen=True)
class LayoutCellEvidence:
    column_index: int
    text: str
    bbox: NormalizedBBox | None
    slot_state: Literal["value", "explicit_empty", "covered", "unknown"] = "value"
    column_span: int = 1
    row_span: int = 1
    covered_by: tuple[int, int] | None = None


@dataclass(slots=True, frozen=True)
class LayoutRowEvidence:
    cells: tuple[LayoutCellEvidence, ...]
    bbox: NormalizedBBox


@dataclass(slots=True, frozen=True)
class LayoutTableEvidence:
    page_number: int
    bbox: NormalizedBBox
    column_bounds: tuple[tuple[float, float], ...]
    rows: tuple[LayoutRowEvidence, ...]


@dataclass(slots=True, frozen=True)
class PageLayoutEvidence:
    page_number: int
    width: float
    height: float
    words: tuple[LayoutWord, ...] = field(default_factory=tuple)
    tables: tuple[LayoutTableEvidence, ...] = field(default_factory=tuple)


class TableLayoutEvidenceProvider(ABC):
    """Provide PDF geometry without deciding table or row relationships."""

    provider_id = "layout"

    async def discover(self, document: Document) -> set[int]:
        """Return one-based pages that may contain tables.

        Providers that cannot discover candidates cheaply may keep the empty
        default and continue serving explicitly requested pages through
        :meth:`collect`.
        """
        return set()

    @abstractmethod
    async def collect(self, document: Document, page_numbers: set[int]) -> list[PageLayoutEvidence]:
        """Collect evidence for the requested one-based page numbers."""
        ...


class DocumentStructureNormalizer(ABC):
    """Normalize parsed structure while retaining the existing document model."""

    @abstractmethod
    async def normalize(
        self,
        document: Document,
        processed_document: ProcessedDocument,
        config: TableReconstructionConfig,
    ) -> ProcessedDocument:
        """Return a processed document with an optional normalized block view."""
        ...


__all__ = [
    "DocumentStructureNormalizer",
    "LayoutCellEvidence",
    "LayoutRowEvidence",
    "LayoutTableEvidence",
    "LayoutWord",
    "NormalizedBBox",
    "PageLayoutEvidence",
    "TableLayoutEvidenceProvider",
]
