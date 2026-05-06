import asyncio
import gc
import re
import time

import pypdfium2
import ray
import torch
from config import load_config
from core.indexing.image_preprocessor import pil_to_png_bytes
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import (
    Document,
    DocumentType,
    ImageBlock,
    ProcessedDocument,
    TextBlock,
)
from marker.converters.pdf import PdfConverter
from utils.logger import get_logger

from ..ray_utils import with_retry, with_timeout

logger = get_logger()
config = load_config()

if torch.cuda.is_available():
    MARKER_NUM_GPUS = config.loader.marker_num_gpus
else:  # On CPU
    MARKER_NUM_GPUS = 0


@ray.remote(num_gpus=MARKER_NUM_GPUS, max_restarts=5)
class MarkerWorker:
    def __init__(self):
        import os

        from config import load_config
        from utils.logger import get_logger

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

        if self.executor:
            self.logger.warning("Resetting ProcessPoolExecutor")
            self.executor.shutdown(wait=False, cancel_futures=True)
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
            future = self.executor.submit(self._process_pdf, file_path, converter_config)
            try:
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
                self.logger.exception("MarkerWorker child process timed out", path=file_path)
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
        """Clean up ProcessPoolExecutor on actor destruction"""
        if self.executor:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass  # Best effort cleanup


@ray.remote(max_restarts=5)
class MarkerPool:
    def __init__(self):
        from config import load_config
        from utils.logger import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.max_processes = self.config.loader.marker_max_processes
        self.pool_size = self.config.loader.marker_pool_size
        self.actors = [MarkerWorker.remote() for _ in range(self.pool_size)]
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

    @with_timeout(
        seconds=config.loader.marker_timeout,
        description="MarkerWorker pool health check",
    )
    async def _check_pool_broken(self, worker):
        return worker.is_pool_broken.remote()

    @with_timeout(
        seconds=config.loader.marker_timeout,
        description="MarkerWorker pool reset",
    )
    async def _reset_worker_pool(self, worker):
        return worker.setup_mp.remote()

    async def ensure_worker_pool_healthy(self, worker):
        if await self._check_pool_broken(worker):
            self.logger.warning("Worker ProcessPoolExecutor is broken. Reinitializing pool...")
            await self._reset_worker_pool(worker)

    @with_timeout(
        seconds=config.loader.marker_timeout,
        description="MarkerPool PDF {label} ({file_path})",
    )
    async def _run_chunk(self, worker, file_path: str, page_range: list[int] | None, label: str):
        return worker.process_pdf.remote(file_path, page_range=page_range)

    @with_retry(
        max_retries=config.loader.marker_max_task_retry,
        base_delay=config.loader.marker_retry_base_delay,
        description="MarkerPool PDF {label} ({file_path})",
    )
    async def _process_chunk(self, file_path: str, page_range: list[int] | None, label: str):
        """Acquire a worker slot, process a PDF chunk, and release the slot.

        A fresh worker is acquired per attempt so a flaky worker can be
        sidestepped and ``ensure_worker_pool_healthy`` re-runs each time.
        Retries are handled by ``@with_retry``.
        """
        worker = await self._queue.get()
        try:
            self.logger.info(f"MarkerWorker allocated for {label}")
            await self.ensure_worker_pool_healthy(worker)
            return await self._run_chunk(worker, file_path, page_range, label)
        finally:
            await self._queue.put(worker)
            self.logger.debug(f"MarkerWorker returned to pool for {label}")

    async def process_pdf(self, file_path: str):
        chunk_size = self.config.loader.marker_chunk_size

        if chunk_size <= 0:
            return await self._process_chunk(file_path, page_range=None, label="(all pages)")

        page_count = self._get_page_count(file_path)
        chunks = self._create_chunks(page_count, chunk_size)

        if len(chunks) == 1:
            page_range, label = chunks[0]
            return await self._process_chunk(file_path, page_range=None, label=label)

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
        self.worker = ray.get_actor("MarkerPool", namespace="openrag")

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

    @with_timeout(
        seconds=config.loader.marker_timeout,
        description="MarkerLoader PDF loading ({file_path})",
    )
    async def _convert_pdf(self, file_path: str):
        return self.worker.process_pdf.remote(file_path)

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
                logger.warning("Failed to encode Marker image %s: %s", key, exc)
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
