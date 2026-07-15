import asyncio
import gc
import re
import threading
import time

import pypdfium2
import ray
import torch
from core.config import load_config
from core.indexing.image_preprocessor import pil_to_png_bytes
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import (
    Document,
    DocumentType,
    ImageBlock,
    ProcessedDocument,
    TextBlock,
)
from core.utils.logging import get_logger
from marker.converters.pdf import PdfConverter

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff

logger = get_logger()


def _force_kill_executor(executor, log) -> None:
    """SIGKILL an executor's worker processes, then shut it down.

    ``ProcessPoolExecutor.shutdown()`` only asks workers to exit *after* their
    current task finishes. A worker wedged on a pathological PDF never finishes,
    so a plain shutdown leaves it running — holding its pool slot and the GPU
    indefinitely (#659). Killing the OS processes directly is the only way to
    reclaim a wedged worker; the whole pool is recycled because
    ``ProcessPoolExecutor`` doesn't expose which worker ran a given task.
    """
    if executor is None:
        return
    # Snapshot before shutdown(): the shutdown machinery clears ``_processes``.
    procs = list(getattr(executor, "_processes", {}).values())
    for proc in procs:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - one unkillable proc must not block the rest
            log.warning("Failed to kill Marker worker process", exc_info=True)
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001 - teardown is best-effort; a fresh pool follows
        log.warning("Failed to shut down Marker executor", exc_info=True)
    for proc in procs:
        try:
            proc.join(timeout=5)
        except Exception:  # noqa: BLE001 - a stuck join must not block the rebuild
            pass


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


# Parser-bomb cap: never process more than this many pages from one PDF, so a
# crafted high-page-count file can't exhaust CPU/memory during ingestion.
# 0 or negative disables the cap.
_MAX_PDF_PAGES = 2000


@ray.remote
class MarkerWorker:
    def __init__(self):
        import os

        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.page_sep = "[PAGE_SEP]"

        self._workers = self.config.loader.marker_max_processes

        self.converter_config = {
            "output_format": "markdown",
            "paginate_output": True,
            "page_separator": self.page_sep,
            "pdftext_workers": self.config.loader.marker_pdftext_workers,
            "disable_multiprocessing": False,
        }
        os.environ["RAY_ADDRESS"] = "auto"

        self.executor = None
        # Serializes executor submit vs. teardown/rebuild: a parse timeout can
        # reset the pool from a worker thread while other threads are submitting.
        self._executor_lock = threading.Lock()
        self.init_resources()

    def init_resources(self):
        from marker.models import create_model_dict

        self.model_dict = create_model_dict()
        for v in self.model_dict.values():
            if hasattr(v.model, "share_memory"):
                v.model.share_memory()

        self.setup_mp()

    def setup_mp(self):
        """Initialize ProcessPoolExecutor for PDF processing.

        We use ProcessPoolExecutor instead of multiprocessing.Pool because:
        - Ray actors run as daemon processes
        - Pool workers are daemonic by default and cannot spawn children
        - The pdftext library (used by Marker) internally spawns processes
        - ProcessPoolExecutor workers are non-daemon, allowing nested process creation
        """
        from concurrent.futures import ProcessPoolExecutor

        import torch.multiprocessing as mp

        with self._executor_lock:
            if self.executor is not None:
                # Force-kill: a wedged worker won't exit on a plain shutdown, so
                # it would keep holding its slot and the GPU (#659).
                self.logger.warning("Resetting ProcessPoolExecutor (killing worker processes)")
                _force_kill_executor(self.executor, self.logger)
                self.executor = None

            # Ensure spawn method for CUDA compatibility
            try:
                if mp.get_start_method(allow_none=True) != "spawn":
                    mp.set_start_method("spawn", force=True)
            except RuntimeError:
                self.logger.warning("Process start method already set, using existing method")

            self.logger.info(f"Initializing MarkerWorker with {self._workers} workers")
            self.executor = ProcessPoolExecutor(
                max_workers=self._workers,
                initializer=self._worker_init,
                initargs=(self.model_dict,),
                mp_context=mp.get_context("spawn"),
                max_tasks_per_child=self.config.loader.marker_max_tasks_per_child,
            )
            self.logger.info("MarkerWorker initialized with ProcessPoolExecutor")

    @staticmethod
    def _worker_init(model_dict):
        global worker_model_dict
        worker_model_dict = model_dict
        logger.debug("Worker initialized with model dictionary")

    @staticmethod
    def _process_pdf(file_path, config):
        global worker_model_dict

        page_range = config.get("page_range")
        if page_range is not None:
            label = f"[p{page_range[0]}-{page_range[-1]}]"
        else:
            label = "(all pages)"

        try:
            logger.debug("Processing PDF", path=file_path, label=label)
            converter = PdfConverter(
                artifact_dict=worker_model_dict,
                config=config,
            )
            render = converter(file_path)
            return render
        except Exception as e:
            logger.exception("Error processing PDF", path=file_path, label=label, error=str(e))
            raise
        finally:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()

    async def process_pdf(self, file_path: str, page_range: list[int] | None = None):
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        converter_config = self.converter_config.copy()
        if page_range is not None:
            converter_config["page_range"] = page_range

        loop = asyncio.get_event_loop()
        timeout = self.config.loader.marker_timeout

        def run_with_timeout():
            with self._executor_lock:
                future = self.executor.submit(self._process_pdf, file_path, converter_config)
            try:
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
                # The child is still computing on the GPU and won't stop on its
                # own; recycle the pool to reclaim the wedged worker's slot so it
                # isn't lost forever (#659). Sibling parses in this worker are
                # recycled too and retried by MarkerPool.
                self.logger.exception(
                    "MarkerWorker child process timed out; recycling the pool to reclaim the slot",
                    path=file_path,
                )
                self.setup_mp()
                raise
            except Exception:
                self.logger.exception("Error processing with MarkerWorker", path=file_path)
                raise

        result = await loop.run_in_executor(None, run_with_timeout)
        return result.markdown, result.images

    def is_pool_broken(self):
        # ProcessPoolExecutor auto-replaces dead/finished workers on next
        # submit(), so counting live processes is unreliable and unnecessary.
        # Only a None or shut-down executor requires reinitialization.
        return self.executor is None or bool(getattr(self.executor, "_broken", False))

    def __del__(self):
        """Clean up ProcessPoolExecutor on actor destruction.

        Force-kill so a worker still wedged on a parse doesn't outlive the actor
        and keep holding the GPU (#659).
        """
        executor = getattr(self, "executor", None)
        if executor:
            try:
                _force_kill_executor(executor, self.logger)
            except Exception:
                pass  # Best effort cleanup


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

    @staticmethod
    def _get_page_count(file_path: str) -> int:
        pdf = pypdfium2.PdfDocument(file_path)
        try:
            return len(pdf)
        finally:
            pdf.close()

    @staticmethod
    def _create_chunks(page_count: int, chunk_size: int) -> list[tuple[list[int], str]]:
        if page_count <= chunk_size:
            return [(list(range(page_count)), f"({page_count}p)")]
        chunks = []
        for start in range(0, page_count, chunk_size):
            end = min(start + chunk_size, page_count)
            page_range = list(range(start, end))
            label = f"[p{start}-{end - 1}]"
            chunks.append((page_range, label))
        return chunks

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

        total_pages = self._get_page_count(file_path)
        capped = _MAX_PDF_PAGES > 0 and total_pages > _MAX_PDF_PAGES
        if capped:
            self.logger.warning(
                f"PDF has {total_pages} pages; processing only the first {_MAX_PDF_PAGES} (max page cap)"
            )
        page_count = min(total_pages, _MAX_PDF_PAGES) if _MAX_PDF_PAGES > 0 else total_pages

        if chunk_size <= 0:
            # When capped, restrict to the first page_count pages instead of all.
            page_range = list(range(page_count)) if capped else None
            label = f"(first {page_count}p)" if capped else "(all pages)"
            return await self._process_chunk(file_path, page_range=page_range, label=label)

        chunks = self._create_chunks(page_count, chunk_size)

        if len(chunks) == 1:
            page_range, label = chunks[0]
            # When capped, the single chunk only covers the first page_count pages,
            # so pass that explicit range — page_range=None would process the whole
            # file and bypass the cap. Uncapped, None means "all pages".
            return await self._process_chunk(file_path, page_range=(page_range if capped else None), label=label)

        self.logger.info(
            f"Splitting {page_count}-page PDF into {len(chunks)} chunks of ~{chunk_size} pages for parallel processing"
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


_MARKER_KEY_PAGE_RE = re.compile(r"_page_(\d+)_")


def _marker_key_to_page(key: str) -> int | None:
    """Extract the 1-indexed page number from a Marker image key.

    Marker emits keys like ``_page_0_Picture_1.jpeg`` (0-indexed). We
    return ``N + 1`` so callers see 1-indexed pages aligned with the
    ``[PAGE_N]`` markers produced by the post-processing step.
    Returns ``None`` if the key doesn't match the expected pattern.
    """
    match = _MARKER_KEY_PAGE_RE.search(key)
    if match is None:
        return None
    try:
        return int(match.group(1)) + 1
    except (TypeError, ValueError):
        return None


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
    _PAGE_MARKER_RE = re.compile(r"\{(\d+)\}" + re.escape(PAGE_SEP))

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
        """Convert Marker's ``{key: PIL_image}`` dict into ``ImageBlock``s.

        Each block records the ``![](key)`` markdown ref in
        ``metadata['markdown_ref']`` so a downstream caption stage can
        substitute the wrapped caption back into the text. The page
        number is parsed from Marker's key format
        (``_page_{N}_Picture_{i}.{ext}``) and stored 1-indexed to match
        the ``[PAGE_N]`` markers in the post-processed markdown.
        """
        blocks: list[ImageBlock] = []
        for key, pil_image in images.items():
            try:
                png_bytes = pil_to_png_bytes(pil_image)
            except Exception as exc:
                logger.warning(f"Failed to encode Marker image {key}: {exc}")
                continue
            blocks.append(
                ImageBlock(
                    image_bytes=png_bytes,
                    page_number=_marker_key_to_page(str(key)),
                    mime_type="image/png",
                    metadata={"markdown_ref": f"![]({key})", "marker_key": str(key)},
                )
            )
        return blocks

    @classmethod
    def _split_pages(cls, markdown: str) -> list[tuple[int, str]]:
        """Clean Marker output and split it into ``[(page_number, text), …]``.

        Marker emits ``<page1>{1}[PAGE_SEP]<page2>{2}[PAGE_SEP]…``. We
        drop the leading ``[PAGE_SEP]`` segment (Marker prefixes one),
        strip ``<br>``, then split on each ``{N}[PAGE_SEP]`` marker —
        the captured ``N`` is the 1-indexed page that just ended.

        Blank pages are preserved (text=``""``) so ``page_number`` and
        ``page_count`` reflect the source document, not just the
        non-empty subset. Trailing text after the last marker (rare) is
        assigned to ``last_page + 1``. Markdown with no markers collapses
        to a single page-1 entry.
        """
        if markdown is None:
            return []
        if cls.PAGE_SEP in markdown:
            markdown = markdown.split(cls.PAGE_SEP, 1)[1]
        markdown = markdown.replace("<br>", "")

        pairs: list[tuple[int, str]] = []
        cursor = 0
        last_page = 0
        for match in cls._PAGE_MARKER_RE.finditer(markdown):
            page = int(match.group(1))
            text = markdown[cursor : match.start()].strip()
            pairs.append((page, text))
            cursor = match.end()
            last_page = page
        tail = markdown[cursor:].strip()
        if tail:
            pairs.append((last_page + 1, tail))
        elif not pairs and markdown.strip():
            pairs.append((1, markdown.strip()))
        return pairs
