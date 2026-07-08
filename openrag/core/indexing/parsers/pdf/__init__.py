"""PDF parser backends.

Each backend lives in its own module so its heavy dependencies (Marker,
Docling, DotsOCR, …) are only imported when the backend's submodule is
itself imported. Consumers do
``from core.indexing.parsers.pdf.marker import MarkerParser`` rather
than going through this package, so importing ``pdf`` does not pull in
any backend.
"""
