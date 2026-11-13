"""Docling-backed PDF Ray actors and BasePooledParser facade.

``DoclingWorker`` and ``DoclingPool`` are Ray actors.
``DoclingLoader`` is a :class:`~core.indexing.parsers.document_parser.BasePooledParser`
that wraps the pool so the core pipeline can call ``parse()`` uniformly.

The old module ``components/indexer/loaders/pdf_loaders/docling2.py`` re-exports
``DoclingWorker`` and ``DoclingPool`` for legacy import paths.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

import ray
import torch
from core.config import load_config
from core.indexing.image_preprocessor import decode_data_uri
from core.indexing.parsers.document_parser import BasePooledParser
from core.indexing.parsers.progress import report_parser_substage
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
from docling_core.transforms.serializer.base import BaseDocSerializer, SerializationResult
from docling_core.transforms.serializer.markdown import (
    MarkdownDocSerializer,
    MarkdownParams,
    MarkdownPictureSerializer,
)
from docling_core.types.doc.document import DoclingDocument, PictureItem

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff

logger = get_logger()

# What docling-core prints where a picture's image goes (``image_placeholder``).
# ``_PictureRefSerializer`` swaps it for the picture's own ref before the page
# text is assembled, so it never reaches the page text.
_IMAGE_PLACEHOLDER = "<!-- image -->"


def _docling_num_gpus(config) -> float:
    """Return Docling's Ray GPU reservation, falling back to CUDA detection.

    Must mirror Marker: ``_docling_num_gpus`` runs inside the ``DoclingPool``
    actor, which Ray schedules with no GPU — so ``torch.cuda.is_available()`` is
    False there (``CUDA_VISIBLE_DEVICES=""``) and a naive local check reserves 0
    GPUs for the worker, leaving Docling stuck on CPU. Query the Ray *cluster*
    for GPU capacity instead, with local CUDA only as a startup fallback.
    """
    requested_gpus = config.loader.docling_num_gpus
    if requested_gpus <= 0:
        return 0
    try:
        return requested_gpus if ray.cluster_resources().get("GPU", 0) > 0 else 0
    except Exception as exc:
        logger.warning("Failed to query Ray cluster GPU resources; falling back to CUDA check", error=str(exc))
        return requested_gpus if torch.cuda.is_available() else 0


class _PictureRefSerializer(MarkdownPictureSerializer):
    """Prints a picture as the ``markdown_ref`` of the ``ImageBlock`` built from it.

    ``refs`` maps a picture's ``self_ref`` to that ref, so the caption stage finds
    each picture's ref exactly where the picture is, whatever else the page
    holds (#1313). A picture with no block, its image unreadable, prints nothing
    in its image's place; its caption, if any, stays.
    """

    def __init__(self, refs: Mapping[str, str]) -> None:
        self._refs = refs

    def serialize(
        self,
        *,
        item: PictureItem,
        doc_serializer: BaseDocSerializer,
        doc: DoclingDocument,
        **kwargs: Any,
    ) -> SerializationResult:
        result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
        # docling-core joins a picture's caption, image and chart table with a
        # blank line, the image part being the placeholder alone.
        ref = self._refs.get(item.self_ref, "")
        parts = [ref if part == _IMAGE_PLACEHOLDER else part for part in result.text.split("\n\n")]
        return result.model_copy(update={"text": "\n\n".join(part for part in parts if part)})


def _page_markdown(document: DoclingDocument, page_no: int, refs: Mapping[str, str]) -> str:
    """``document.export_to_markdown(page_no=page_no)``, each picture printed as its ref."""
    serializer = MarkdownDocSerializer(
        doc=document,
        picture_serializer=_PictureRefSerializer(refs),
        params=MarkdownParams(pages={page_no}, image_placeholder=_IMAGE_PLACEHOLDER),
    )
    return serializer.serialize().text.strip()


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


# max_restarts mirrors MarkerPool: the detached pool supervisor must survive its
# own crashes rather than staying permanently dead (#722). Ray's default is 0.
@ray.remote(max_restarts=5)
class DoclingPool:
    def __init__(self):
        self.logger = get_logger()
        self.config = load_config()
        self.pool_size = self.config.loader.docling_pool_size
        self.max_tasks_per_worker = self.config.loader.docling_max_tasks_per_worker

        # max_restarts mirrors MarkerWorker: a worker that OOMs/CUDA-faults is
        # restarted in place (same actor id), so the handle already queued keeps
        # working instead of every retry drawing the same dead handle (#722).
        self.actors = [
            DoclingWorker.options(num_gpus=_docling_num_gpus(self.config), max_restarts=5).remote()
            for _ in range(self.pool_size)
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
    per picture.  Image captioning is left to the downstream caption stage:
    each picture is printed in its page's text as the ref of its block, and
    each block keeps its picture's page (``prov[0].page_no``), so the stage can
    land every caption where the figure was.
    """

    def __init__(self) -> None:
        self.config = load_config()

    def _pool(self) -> DoclingPool:
        """Look up the named ``DoclingPool`` actor; call once per dispatch.

        Never cached: this loader lives as long as its indexer worker, and
        ``POST /actors/DoclingPool/restart`` replaces the pool with a new actor,
        so a cached handle would keep dispatching to the killed one. Store the
        result before calling a method on it: Ray's ``ActorMethod`` holds its
        handle weakly, so ``self._pool().process_pdf.remote()`` raises
        "Lost reference to actor".
        """
        # Create the pool lazily if bootstrap didn't: bootstrap only pre-warms the
        # globally-configured PDF backend, but a per-preset parsing_strategy can
        # select docling even when the global default is marker (see #569/#575).
        from services.workers.bootstrap import get_or_create_actor

        return get_or_create_actor("DoclingPool", DoclingPool, lifetime="detached")

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        report_parser_substage("parse.convert", "Converting PDF with Docling")
        async with document.as_temporary_file() as path:
            result: ConversionResult = await self._dispatch(str(path))

        # Exporting each page re-walks the whole document: it can take minutes of
        # CPU on a large PDF. On the loop that starves the lease renewals of every
        # other file on this worker.
        # A timeout or cancel cannot interrupt the thread; the event tells it to stop.
        cancelled = threading.Event()
        try:
            text_blocks, image_blocks = await asyncio.to_thread(self._postprocess, result, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=image_blocks,
            metadata=dict(document.metadata),
            page_count=len(result.pages),
        )

    def _postprocess(
        self, result: ConversionResult, cancelled: threading.Event
    ) -> tuple[list[TextBlock], list[ImageBlock]]:
        """Build the text and image blocks off the loop.

        The image blocks are built first: the page text prints each picture as
        the ref of its block, looked up by the picture's ``self_ref``.

        ``cancelled`` is checked before each page and each picture: once set,
        the caller has gone and the result is discarded, so the work stops early.
        """
        blocks: list[ImageBlock] = []
        refs: dict[str, str] = {}
        for idx, picture in enumerate(result.document.pictures):
            if cancelled.is_set():
                break
            # Docling indexes pages from 1, like OpenRAG's TextBlock/Marker blocks.
            page_no = picture.prov[0].page_no if picture.prov else None
            block = self._build_image_block(picture, idx, page_no)
            if block is not None:
                refs[picture.self_ref] = block.metadata["markdown_ref"]
                blocks.append(block)
        return self._build_text_blocks(result, refs, cancelled), blocks

    @staticmethod
    def _build_image_block(picture: Any, idx: int, page_no: int | None) -> ImageBlock | None:
        """The ``ImageBlock`` of one picture, without encoding it; ``None`` if unreadable.

        Docling PNG-encoded each picture into its ``data:`` URI when it generated
        it, so those bytes are reused as they are. A picture without one keeps its
        decoded image in ``pil_image``, which the caption stage encodes only when
        it captions the document.
        """
        try:
            uri = str(getattr(picture.image, "uri", None) or "")
            png_bytes = decode_data_uri(uri) if uri.startswith("data:image/png;") else None
            pil_image = None if png_bytes else picture.image.pil_image
        except Exception as exc:
            logger.warning(f"Failed to read Docling picture {idx}: {exc}")
            return None
        if not png_bytes and pil_image is None:
            return None
        return ImageBlock(
            image_bytes=png_bytes or b"",
            pil_image=pil_image,
            page_number=page_no,
            mime_type="image/png",
            metadata={"markdown_ref": f"![](docling_img_{idx})"},
        )

    async def _dispatch(self, file_path: str) -> ConversionResult:
        pool = self._pool()
        return await call_ray_actor_with_timeout(
            pool.process_pdf.remote(file_path),
            timeout=self.config.loader.docling_timeout,
            task_description=f"DoclingLoader PDF loading ({file_path})",
        )

    @staticmethod
    def _build_text_blocks(
        result: ConversionResult,
        refs: Mapping[str, str] | None = None,
        cancelled: threading.Event | None = None,
    ) -> list[TextBlock]:
        """One ``TextBlock`` per page, each picture printed as its ref (``refs``).

        ``export_to_markdown`` would print ``<!-- image -->`` for every picture,
        which the caption stage never looks for: each caption was appended at
        the end of the document, attributed to the last page, and the marker
        was indexed (#1313).

        The pages are the ones the document holds, by their own number: a page
        Docling left out leaves a gap, so counting ``result.pages`` from 1
        would export a missing page and skip the last one.
        """
        blocks: list[TextBlock] = []
        page_numbers = sorted(result.document.pages)
        n_pages = len(page_numbers)
        refs = refs or {}
        if n_pages:
            report_parser_substage("parse.text", "Extracting page text", progress=0, total=n_pages)
        else:
            report_parser_substage("parse.text", "Extracting page text")
        for done, page_no in enumerate(page_numbers, start=1):
            if cancelled is not None and cancelled.is_set():
                break
            text = _page_markdown(result.document, page_no, refs)
            blocks.append(TextBlock(text=text, page_number=page_no))
            report_parser_substage("parse.text", "Extracting page text", progress=done, total=n_pages)
        return blocks


__all__ = ["DoclingLoader", "DoclingPool", "DoclingWorker"]
