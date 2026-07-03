import asyncio
import time

import ray
import torch
from core.config import load_config
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import (
    Document,
    DocumentType,
    ImageBlock,
    ProcessedDocument,
    TextBlock,
)
from core.utils.logging import get_logger

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff
from . import marker_format
from .marker_engine import MAX_PDF_PAGES, MarkerEngine, create_chunks, page_count

logger = get_logger()


def _marker_num_gpus(config) -> float:
    """Return Marker's Ray GPU reservation, falling back to CUDA detection.

    Ray scheduling must see GPU capacity before an actor can request a GPU
    fraction. If Ray cannot report cluster resources yet, use local CUDA
    availability as the fallback so single-node startup still honors the
    configured Marker GPU request.
    """
    requested_gpus = config.loader.marker_num_gpus
    if requested_gpus <= 0:
        return 0
    try:
        return requested_gpus if ray.cluster_resources().get("GPU", 0) > 0 else 0
    except Exception as exc:
        logger.warning("Failed to query Ray cluster GPU resources; falling back to CUDA check", error=str(exc))
        return requested_gpus if torch.cuda.is_available() else 0


@ray.remote
class MarkerWorker:
    """Ray actor wrapping a :class:`MarkerEngine`.

    Thin delegation layer: model loading, the ProcessPoolExecutor, and per-unit
    conversion all live in the Ray-free engine so the marker-serve HTTP worker
    reuses the exact same code. Behaviour of this actor is unchanged.
    """

    def __init__(self):
        import os

        self.config = load_config()
        # Marker's pdftext children re-attach to the Ray runtime for logging.
        os.environ["RAY_ADDRESS"] = "auto"
        self.engine = MarkerEngine(self.config)

    async def process_pdf(self, file_path: str, page_range: list[int] | None = None):
        return await self.engine.process_pdf(file_path, page_range=page_range)

    def is_pool_broken(self):
        return self.engine.is_broken()

    def setup_mp(self):
        self.engine.reset()

    def __del__(self):
        engine = getattr(self, "engine", None)
        if engine is not None:
            engine.close()


@ray.remote(max_restarts=5)
class MarkerPool:
    def __init__(self):
        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.max_processes = self.config.loader.marker_max_processes
        self.pool_size = self.config.loader.marker_pool_size
        self.actors = [
            MarkerWorker.options(num_gpus=_marker_num_gpus(self.config), max_restarts=5).remote()
            for _ in range(self.pool_size)
        ]
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(self.max_processes):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        self.logger.info(
            f"Marker pool: {self.pool_size} actors × {self.max_processes} slots = "
            f"{self.pool_size * self.max_processes} PDF concurrency"
        )

    async def _check_pool_broken(self, worker):
        return await call_ray_actor_with_timeout(
            worker.is_pool_broken.remote(),
            timeout=self.config.loader.marker_timeout,
            task_description="MarkerWorker pool health check",
        )

    async def _reset_worker_pool(self, worker):
        return await call_ray_actor_with_timeout(
            worker.setup_mp.remote(),
            timeout=self.config.loader.marker_timeout,
            task_description="MarkerWorker pool reset",
        )

    async def ensure_worker_pool_healthy(self, worker):
        if await self._check_pool_broken(worker):
            self.logger.warning("Worker ProcessPoolExecutor is broken. Reinitializing pool...")
            await self._reset_worker_pool(worker)

    async def _run_chunk(self, worker, file_path: str, page_range: list[int] | None, label: str):
        return await call_ray_actor_with_timeout(
            worker.process_pdf.remote(file_path, page_range=page_range),
            timeout=self.config.loader.marker_timeout,
            task_description=f"MarkerPool PDF {label} ({file_path})",
        )

    async def _process_chunk(self, file_path: str, page_range: list[int] | None, label: str):
        """Acquire a worker slot, process a PDF chunk, and release the slot.

        A fresh worker is acquired per attempt so a flaky worker can be
        sidestepped and ``ensure_worker_pool_healthy`` re-runs each time.
        Retries are handled by ``retry_with_backoff``.
        """

        async def attempt(_i: int):
            worker = await self._queue.get()
            try:
                self.logger.info(f"MarkerWorker allocated for {label}")
                await self.ensure_worker_pool_healthy(worker)
                return await self._run_chunk(worker, file_path, page_range, label)
            finally:
                await self._queue.put(worker)
                self.logger.debug(f"MarkerWorker returned to pool for {label}")

        return await retry_with_backoff(
            attempt,
            max_retries=self.config.loader.marker_max_task_retry,
            base_delay=self.config.loader.marker_retry_base_delay,
            task_description=f"MarkerPool PDF {label} ({file_path})",
        )

    async def process_pdf(self, file_path: str):
        chunk_size = self.config.loader.marker_chunk_size

        total_pages = page_count(file_path)
        capped = MAX_PDF_PAGES > 0 and total_pages > MAX_PDF_PAGES
        if capped:
            self.logger.warning(
                f"PDF has {total_pages} pages; processing only the first {MAX_PDF_PAGES} (max page cap)"
            )
        pages = min(total_pages, MAX_PDF_PAGES) if MAX_PDF_PAGES > 0 else total_pages

        if chunk_size <= 0:
            # When capped, restrict to the first `pages` pages instead of all.
            page_range = list(range(pages)) if capped else None
            label = f"(first {pages}p)" if capped else "(all pages)"
            return await self._process_chunk(file_path, page_range=page_range, label=label)

        chunks = create_chunks(pages, chunk_size)

        if len(chunks) == 1:
            page_range, label = chunks[0]
            # When capped, the single chunk only covers the first `pages` pages,
            # so pass that explicit range — page_range=None would process the whole
            # file and bypass the cap. Uncapped, None means "all pages".
            return await self._process_chunk(file_path, page_range=(page_range if capped else None), label=label)

        self.logger.info(
            f"Splitting {pages}-page PDF into {len(chunks)} chunks of ~{chunk_size} pages for parallel processing"
        )

        tasks = [asyncio.create_task(self._process_chunk(file_path, page_range, label)) for page_range, label in chunks]
        try:
            results = await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Reassemble: concatenate markdown in order, merge image dicts
        all_markdown = []
        all_images = {}
        for markdown, images in results:
            all_markdown.append(markdown)
            all_images.update(images)

        combined_markdown = "\n\n".join(all_markdown)
        return combined_markdown, all_images


class MarkerLoader(BasePooledParser):
    """Public ``BasePooledParser`` facade for the Marker Ray pool.

    Holds a handle to the named ``MarkerPool`` Ray actor and dispatches
    each ``parse()`` call to it. Marker requires a file path on disk, so
    ``Document.raw_bytes`` is materialized to a temporary file (via
    ``Document.as_temporary_file``) before handoff.

    Output: one ``TextBlock`` per page (1-indexed ``page_number``) plus
    one ``ImageBlock`` per Marker image. Each ``ImageBlock`` carries the
    ``![](key)`` markdown ref in ``metadata['markdown_ref']`` so a
    downstream caption stage can substitute the wrapped caption back
    into the markdown by string match. Captioning is not done here —
    see :class:`ImageBlock` for the parser→caption contract.
    """

    PAGE_SEP = "[PAGE_SEP]"

    def __init__(self) -> None:
        self.config = load_config()
        # Lazily create the pool if bootstrap didn't (it only pre-warms the
        # globally-configured PDF backend; a preset can select marker even when
        # the global default is docling — see #569/#575).
        from services.workers.bootstrap import get_or_create_actor

        self.worker = get_or_create_actor("MarkerPool", MarkerPool, lifetime="detached")

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            markdown, images = await self._dispatch(str(path))

        pages = self._split_pages(markdown)
        image_blocks = self._build_image_blocks(images)
        text_blocks = [TextBlock(text=text, page_number=page) for page, text in pages]

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=image_blocks,
            metadata=dict(document.metadata),
            page_count=pages[-1][0] if pages else 0,
        )

    # ----- helpers -----

    async def _convert_pdf(self, file_path: str):
        return await call_ray_actor_with_timeout(
            self.worker.process_pdf.remote(file_path),
            timeout=self.config.loader.marker_timeout,
            task_description=f"MarkerLoader PDF loading ({file_path})",
        )

    async def _dispatch(self, file_path: str) -> tuple[str, dict]:
        start = time.time()
        try:
            markdown, images = await self._convert_pdf(file_path)
            if not markdown:
                raise RuntimeError(f"Conversion failed for {file_path}")
            duration = time.time() - start
            logger.info(f"Processed {file_path} in {duration:.2f}s")
            return markdown, images or {}
        except Exception:
            logger.exception("Error in MarkerLoader.parse", path=file_path)
            raise

    @staticmethod
    def _build_image_blocks(images: dict) -> list[ImageBlock]:
        # {key: PIL_image} -> ImageBlocks (shared with the off-Ray client)
        return marker_format.build_image_blocks(images)

    @classmethod
    def _split_pages(cls, markdown: str) -> list[tuple[int, str]]:
        return marker_format.split_pages(markdown)
