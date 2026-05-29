"""Docling-backed PDF Ray actors and BasePooledParser facade.

``DoclingWorker`` and ``DoclingPool`` are Ray actors.
``DoclingLoader`` is a :class:`~core.indexing.parsers.document_parser.BasePooledParser`
that wraps the pool so the core pipeline can call ``parse()`` uniformly.

The old module ``components/indexer/loaders/pdf_loaders/docling2.py`` re-exports
``DoclingWorker`` and ``DoclingPool`` for legacy import paths.
"""

from __future__ import annotations

import asyncio

import ray
import torch
from config import load_config
from core.indexing.image_preprocessor import pil_to_png_bytes
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from core.utils.logging import get_logger
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff

logger = get_logger()


def _docling_num_gpus(config) -> float:
    return config.loader.docling_num_gpus if torch.cuda.is_available() else 0


@ray.remote
class DoclingWorker:
    def __init__(self):
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
        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend)
            }
        )

    async def convert(self, file_path: str) -> ConversionResult:
        with torch.no_grad():
            return await asyncio.to_thread(self.converter.convert, str(file_path))


@ray.remote
class DoclingPool:
    def __init__(self):
        self.logger = get_logger()
        self.config = load_config()
        self.pool_size = self.config.loader.docling_pool_size
        self.max_tasks_per_worker = self.config.loader.docling_max_tasks_per_worker

        self.actors = [
            DoclingWorker.options(num_gpus=_docling_num_gpus(self.config)).remote() for _ in range(self.pool_size)
        ]
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(self.max_tasks_per_worker):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        total_slots = self.pool_size * self.max_tasks_per_worker
        self.logger.info(
            f"Docling pool: {self.pool_size} actors × {self.max_tasks_per_worker} slots = {total_slots} PDF concurrency"
        )

    async def process_pdf(self, file_path: str) -> ConversionResult:
        timeout = self.config.loader.docling_timeout

        async def attempt(i: int) -> ConversionResult:
            actor: DoclingWorker = await self._queue.get()
            try:
                return await call_ray_actor_with_timeout(
                    actor.convert.remote(file_path),
                    timeout=timeout,
                    task_description=f"DoclingPool PDF ({file_path})",
                )
            finally:
                await self._queue.put(actor)

        return await retry_with_backoff(
            attempt,
            max_retries=self.config.loader.docling_max_task_retry,
            base_delay=self.config.loader.docling_retry_base_delay,
            task_description=f"DoclingPool PDF ({file_path})",
        )


class DoclingLoader(BasePooledParser):
    """``BasePooledParser`` facade over the Docling Ray pool.

    Materialises ``Document.raw_bytes`` to a temporary file, dispatches to the
    named ``DoclingPool`` actor, and converts the ``ConversionResult`` into a
    ``ProcessedDocument`` with one ``TextBlock`` per page and one ``ImageBlock``
    per picture.  Image captioning is left to the downstream caption stage.
    """

    def __init__(self) -> None:
        self.config = load_config()
        self.worker: DoclingPool = ray.get_actor("DoclingPool", namespace="openrag")

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            result: ConversionResult = await self._dispatch(str(path))

        text_blocks = self._build_text_blocks(result)
        image_blocks = self._build_image_blocks(result)

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=image_blocks,
            metadata=dict(document.metadata),
            page_count=len(result.pages),
        )

    async def _dispatch(self, file_path: str) -> ConversionResult:
        return await call_ray_actor_with_timeout(
            self.worker.process_pdf.remote(file_path),
            timeout=self.config.loader.docling_timeout,
            task_description=f"DoclingLoader PDF loading ({file_path})",
        )

    @staticmethod
    def _build_text_blocks(result: ConversionResult) -> list[TextBlock]:
        blocks: list[TextBlock] = []
        n_pages = len(result.pages)
        for i in range(1, n_pages + 1):
            text = result.document.export_to_markdown(page_no=i).strip()
            blocks.append(TextBlock(text=text, page_number=i))
        return blocks

    @staticmethod
    def _build_image_blocks(result: ConversionResult) -> list[ImageBlock]:
        blocks: list[ImageBlock] = []
        for idx, picture in enumerate(result.document.pictures):
            try:
                pil_image = picture.image.pil_image
                if pil_image is None:
                    continue
                png_bytes = pil_to_png_bytes(pil_image)
            except Exception as exc:
                logger.warning(f"Failed to encode Docling picture {idx}: {exc}")
                continue
            ref = f"![](docling_img_{idx})"
            blocks.append(
                ImageBlock(
                    image_bytes=png_bytes,
                    mime_type="image/png",
                    metadata={"markdown_ref": ref},
                )
            )
        return blocks


__all__ = ["DoclingLoader", "DoclingPool", "DoclingWorker"]
