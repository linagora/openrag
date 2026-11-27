"""The conversion a PDF loader hands a file to.

``MarkerLoader`` and ``DoclingLoader`` turn a converted PDF into pages and
image blocks; how the file is converted is their conversion's business. Today
that is a Ray pool (``RayMarkerConversion``, ``RayDoclingConversion``). The
loaders' post-processing is shared by every conversion, so their output can't
differ between them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from docling.datamodel.document import ConversionResult


class MarkerConversion(Protocol):
    async def convert(self, file_path: str) -> tuple[str, dict[str, Any]]:
        """Marker's markdown of the PDF at *file_path*, and its images by key.

        Raises ``MemoryError`` when the conversion ran out of memory.
        """
        ...


class DoclingConversion(Protocol):
    async def convert(self, file_path: str) -> ConversionResult:
        """Docling's conversion of the PDF at *file_path*."""
        ...


__all__ = ["DoclingConversion", "MarkerConversion"]
