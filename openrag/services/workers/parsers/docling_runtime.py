"""What a Docling conversion needs, without Ray.

``DoclingWorker`` builds its converter here in a Ray actor; an engine running
in its own process builds the same one, so both convert with the same options.
"""

from __future__ import annotations

from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption


def build_converter() -> DocumentConverter:
    """A converter with OCR, accurate tables and picture images, on the best device available."""
    img_scale = 2
    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        generate_picture_images=True,
        images_scale=img_scale,
    )
    pipeline_options.table_structure_options = TableStructureOptions(
        do_cell_matching=True, mode=TableFormerMode.ACCURATE
    )
    pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO)
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend)
        }
    )


__all__ = ["build_converter"]
