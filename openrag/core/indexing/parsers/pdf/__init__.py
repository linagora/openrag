"""PDF parser backends.

Each backend lives in its own module so its heavy dependencies (Marker,
Docling, DotsOCR, …) are only imported when the backend is actually
instantiated.
"""

from .client_based import ClientPdfParser
from .pymupdf import PyMuPDFParser

__all__ = ["ClientPdfParser", "PyMuPDFParser"]
