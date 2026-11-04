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
from typing import Any

import ray
import torch
from core.config import load_config
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

# The token ``export_to_markdown`` prints for every picture whose bytes it does
# not inline (its ``image_placeholder`` default in docling-core). OpenRAG never
# ships the picture bytes inside the markdown either; the caption stage instead
# substitutes these occurrences with the ``markdown_ref`` of the ``ImageBlock``
# built from the very same picture, which keeps every caption anchored where
# its figure was.
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


def _swap_image_placeholders(text: str, slots: list[str | None]) -> str:
    """Replace the k-th ``<!-- image -->`` with the k-th ref, in document order.

    Docling's exporter prints one ``_IMAGE_PLACEHOLDER`` occurrence per picture
    it walked (``document.pictures`` is appended to in the same traversal order
    the exporter emits elements in), so the k-th occurrence of a page and the
    k-th picture of that page correspond one-to-one. Matching by position is
    what keeps a multi-image page ordered: ``str.replace`` would have stamped
    the same ref onto every placeholder.

    ``slots`` holds one entry per picture of that page, in traversal order —
    the picture's ref when it encoded, ``None`` when it did not. Mismatches
    degrade safely instead of ever misassigning:

    - a ``None`` slot (picture failed to encode or render) keeps its own
      placeholder retired — the token is inert, and no ref points at a
      nonexistent ``ImageBlock``; later images still land at their own
      occurrence because the slot list stays aligned with the pictures;
    - more occurrences than slots (a picture the exporter rendered some other
      way, a picture with no page to export on): the surplus occurrences are
      left as-is;
    - more slots than occurrences (e.g. a chart exported as a table instead
      of a picture): the surplus pictures get no inline position, and the
      caption stage appends their captions with their own page number.
    """
    parts = text.split(_IMAGE_PLACEHOLDER)
    if len(parts) == 1:
        return text  # nothing to substitute
    out = [parts[0]]
    for position, part in enumerate(parts[1:]):
        ref = slots[position] if position < len(slots) else None
        out.append(ref if ref is not None else _IMAGE_PLACEHOLDER)
        out.append(part)
    return "".join(out)


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
    each page's exported ``<!-- image -->`` placeholders are swapped for the
    refs of that page's picture blocks in document order, and each block keeps
    its picture's original page (``prov[0].page_no``), so the stage can land
    every caption where the figure was.
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

        async with document.as_temporary_file() as path:
            result: ConversionResult = await self._dispatch(str(path))

        # Exporting each page re-walks the whole document and every picture is
        # PNG-encoded: it can take minutes of CPU on a large PDF. On the loop that
        # starves the lease renewals of every other file on this worker.
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

        One walk over ``result.document.pictures`` produces both the image
        blocks and the per-page ref slots the text stage substitutes with:
        the k-th slot of a page is that page's k-th picture in traversal
        order — the same order the exporter prints ``<!-- image -->`` in —
        holding that picture's ref when it encoded and ``None`` when it did
        not. A failing picture therefore keeps its own placeholder position
        instead of shifting every later caption one figure early (#1313).

        ``cancelled`` is checked before each page and each picture: once set,
        the caller has gone and the result is discarded, so the work stops early.
        """
        blocks: list[ImageBlock] = []
        slots_by_page: dict[int, list[str | None]] = {}
        for idx, picture in enumerate(result.document.pictures):
            if cancelled.is_set():
                break
            provenance = getattr(picture, "prov", None) or []
            # Docling indexes pages from 1, like OpenRAG's TextBlock/Marker blocks.
            page_no: int | None = provenance[0].page_no if provenance else None
            png_bytes = self._encode_picture(picture, idx)
            ref: str | None = None
            if png_bytes is not None:
                ref = f"![](docling_img_{idx})"
                blocks.append(
                    ImageBlock(
                        image_bytes=png_bytes,
                        page_number=page_no,
                        mime_type="image/png",
                        metadata={"markdown_ref": ref},
                    )
                )
            if page_no is not None:
                slots_by_page.setdefault(page_no, []).append(ref)
        return self._build_text_blocks(result, slots_by_page, cancelled), blocks

    @staticmethod
    def _encode_picture(picture: Any, idx: int) -> bytes | None:
        """PNG bytes for a Docling picture, or ``None`` when it cannot render.

        A picture without a rendered blob (a degraded parse, a chart exported
        as a table) stays silent — the caller reserves ``None`` for its slot —
        while an encode error is logged like before. Keeping both outcomes in
        one helper is what lets ``_postprocess`` reserve the picture's slot
        either way, so a failure never shifts later images onto earlier
        placeholders.
        """
        try:
            pil_image = picture.image.pil_image
            if pil_image is None:
                return None
            return pil_to_png_bytes(pil_image)
        except Exception as exc:
            logger.warning(f"Failed to encode Docling picture {idx}: {exc}")
            return None

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
        slots_by_page: dict[int, list[str | None]] | None = None,
        cancelled: threading.Event | None = None,
    ) -> list[TextBlock]:
        """One ``TextBlock`` per page, with image placeholders swapped for refs.

        ``export_to_markdown`` prints ``<!-- image -->`` for every picture on
        the page. Left untouched, those occurrences are searched for by the
        caption stage in vain — Docling pictures are not exported with usable
        inline refs — so every caption used to be appended at the end of the
        document, detached from its figure and attributed to the last page
        (#1313). The k-th occurrence of a page is swapped for the k-th slot of
        that page (``slots_by_page``, in document order — see ``_postprocess``),
        which is the ref of the block built from the k-th picture of that page;
        the caption stage then substitutes the wrapped caption for the ref,
        landing where the figure was.

        The substitution only fires on exact alignment: the page's placeholder
        occurrences must match its slot count. Any other number means the
        positional model does not hold for that page — empty provenance on a
        picture, a chart exported as a table, a placeholder without a picture —
        and swapping positions could assign a caption to the wrong figure, so
        the page's placeholders are left untouched instead. Captions then fall
        back to the append path — with their own (now correct) page number,
        never a wrong one.
        """
        blocks: list[TextBlock] = []
        n_pages = len(result.pages)
        slots_by_page = slots_by_page or {}
        for i in range(1, n_pages + 1):
            if cancelled is not None and cancelled.is_set():
                break
            text = result.document.export_to_markdown(page_no=i).strip()
            slots = slots_by_page.get(i, [])
            if text != "" and text.count(_IMAGE_PLACEHOLDER) == len(slots):
                text = _swap_image_placeholders(text, slots)
            elif text.count(_IMAGE_PLACEHOLDER) and slots:
                logger.bind(page=i).debug(
                    f"Page {i}: {text.count(_IMAGE_PLACEHOLDER)} image placeholders "
                    f"vs {len(slots)} picture slots — leaving placeholders in place"
                )
            blocks.append(TextBlock(text=text, page_number=i))
        return blocks


__all__ = ["DoclingLoader", "DoclingPool", "DoclingWorker"]
