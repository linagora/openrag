import asyncio
import os
import re
import threading
import time

import ray
import torch
from core.config import load_config
from core.indexing.parsers.document_parser import BasePooledParser
from core.indexing.parsers.progress import report_parser_substage
from core.models.document import (
    Document,
    DocumentType,
    ImageBlock,
    ProcessedDocument,
    TextBlock,
)
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError

from ..ray_utils import call_ray_actor_with_timeout, caused_by, retry_with_backoff
from . import marker_runtime
from .marker_runtime import (
    _MAX_PDF_PAGES,
    PAGE_SEP,
    _apply_parse_memory_limit,
    _as_parse_memory_error,
    _force_kill_executor,
    converter_config,
)
from .pdf_conversion import MarkerConversion

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


# How many times a pool-recycle retries after its own reset call is cancelled
# (rather than failing) before the worker slot is dropped for good.
_RECYCLE_CANCEL_RETRIES = 3
_RECYCLE_CANCEL_RETRY_DELAY = 0.5


@ray.remote
class MarkerWorker:
    def __init__(self):
        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.page_sep = PAGE_SEP

        self._workers = self.config.loader.marker_max_processes

        self.converter_config = converter_config(self.config)
        if self.config.loader.marker_pdftext_workers > 1:
            self.logger.warning(
                f"MARKER_PDFTEXT_WORKERS={self.config.loader.marker_pdftext_workers} is ignored: "
                "Marker runs pdftext with one worker. Remove the setting."
            )
        os.environ["RAY_ADDRESS"] = "auto"

        # One single-process executor per slot. MarkerPool hands each slot to at
        # most one chunk at a time, so recycling a slot (cancel, timeout, broken
        # pool) kills only the child running that slot's chunk — never a
        # concurrent parse from another file on the same actor.
        self.executors = [None] * self._workers
        # Serializes a slot's submit vs. its teardown/rebuild: a parse timeout can
        # reset the slot from a worker thread while MarkerPool is resetting it too.
        self._executor_locks = [threading.Lock() for _ in range(self._workers)]
        self.init_resources()

    def init_resources(self):
        from marker.models import create_model_dict

        self.model_dict = create_model_dict()
        for v in self.model_dict.values():
            if hasattr(v.model, "share_memory"):
                v.model.share_memory()

        for slot in range(self._workers):
            self.setup_mp(slot)
        self.logger.info(f"MarkerWorker initialized with {self._workers} single-process executors")

    def setup_mp(self, slot: int, old_executor=None):
        """Initialize (or rebuild) the ProcessPoolExecutor backing one slot.

        We use ProcessPoolExecutor instead of multiprocessing.Pool because:
        - Ray actors run as daemon processes
        - Pool workers are daemonic by default and cannot spawn children
        - Libraries used in a parse may start processes of their own
        - ProcessPoolExecutor workers are non-daemon, allowing nested process creation

        ``old_executor`` guards against concurrent resets cascading: a timeout
        handler passes the executor it timed out on, and if the slot has already
        been recycled since then (its executor has moved on), we skip — otherwise
        the second handler would force-kill the fresh executor the first just
        built. ``None`` (init / explicit slot reset) always rebuilds.
        """
        from concurrent.futures import ProcessPoolExecutor

        import torch.multiprocessing as mp

        with self._executor_locks[slot]:
            if old_executor is not None and self.executors[slot] is not old_executor:
                # The slot was already recycled; nothing wedged to reclaim.
                return

            if self.executors[slot] is not None:
                # Force-kill: a wedged worker won't exit on a plain shutdown, so
                # it would keep holding its slot and the GPU (#659).
                self.logger.warning(f"Resetting ProcessPoolExecutor for slot {slot} (killing its worker process)")
                _force_kill_executor(self.executors[slot], self.logger)
                self.executors[slot] = None

            # Ensure spawn method for CUDA compatibility
            try:
                if mp.get_start_method(allow_none=True) != "spawn":
                    mp.set_start_method("spawn", force=True)
            except RuntimeError:
                self.logger.warning("Process start method already set, using existing method")

            self.executors[slot] = ProcessPoolExecutor(
                max_workers=1,
                initializer=self._worker_init,
                initargs=(self.model_dict, self.config.loader.marker_parse_memory_limit_mb, os.getpid()),
                mp_context=mp.get_context("spawn"),
                max_tasks_per_child=self.config.loader.marker_max_tasks_per_child,
            )
            self.logger.debug(f"MarkerWorker slot {slot} executor ready")

    # The child's initializer and conversion, in one module without Ray: they
    # share the models the initializer stores.
    _worker_init = staticmethod(marker_runtime.init_child)
    _process_pdf = staticmethod(marker_runtime.convert_in_child)

    async def process_pdf(self, file_path: str, page_range: list[int] | None = None, *, slot: int):
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        converter_config = self.converter_config.copy()
        if page_range is not None:
            converter_config["page_range"] = page_range

        loop = asyncio.get_event_loop()
        # Expires before the bounds wrapping this call, so the recycle below
        # actually runs instead of being cancelled from outside (#894).
        timeout = self.config.loader.marker_child_timeout

        # Runs on the event loop's default thread pool, whose threads live as
        # long as the actor. A slot's child is started either here by
        # ``submit``, or by the executor's manager thread when it replaces a
        # child after ``max_tasks_per_child``; both threads outlive the child.
        # The child is tied to the thread that started it (``tie_to_parent``):
        # a short-lived thread would take the child with it when it ends.
        def run_with_timeout():
            with self._executor_locks[slot]:
                current_executor = self.executors[slot]
                future = current_executor.submit(self._process_pdf, file_path, converter_config)
            try:
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
                # The child is still computing on the GPU and won't stop on its
                # own; recycle this slot's executor to reclaim it so the slot
                # isn't lost forever (#659). Other slots keep parsing. Pass the
                # executor we timed out on so a concurrent reset of this slot
                # can't kill an executor that was already rebuilt in the meantime.
                self.logger.exception(
                    "MarkerWorker child process timed out; recycling its slot",
                    path=file_path,
                    slot=slot,
                )
                self.setup_mp(slot, old_executor=current_executor)
                raise
            except Exception:
                self.logger.exception("Error processing with MarkerWorker", path=file_path)
                raise

        result = await loop.run_in_executor(None, run_with_timeout)
        return result.markdown, result.images

    def is_pool_broken(self, slot: int):
        # ProcessPoolExecutor auto-replaces dead/finished workers on next
        # submit(), so counting live processes is unreliable and unnecessary.
        # Only a None or shut-down executor requires reinitialization.
        executor = self.executors[slot]
        return executor is None or bool(getattr(executor, "_broken", False))

    def __del__(self):
        """Clean up every slot's ProcessPoolExecutor on actor destruction.

        Force-kill so a worker still wedged on a parse doesn't outlive the actor
        and keep holding the GPU (#659).
        """
        for executor in getattr(self, "executors", None) or []:
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
        # A slot is ``(actor, slot index)``: the index picks that actor's
        # single-process executor, so a slot recycle only touches its own child.
        self._queue: asyncio.Queue[tuple[ray.actor.ActorHandle, int]] = asyncio.Queue()

        for slot in range(self.max_processes):
            for actor in self.actors:
                self._queue.put_nowait((actor, slot))

        self.logger.info(
            f"Marker pool: {self.pool_size} actors × {self.max_processes} slots = "
            f"{self.pool_size * self.max_processes} PDF concurrency"
        )

    _get_page_count = staticmethod(marker_runtime.page_count)
    _create_chunks = staticmethod(marker_runtime.create_chunks)

    async def _check_pool_broken(self, worker):
        actor, slot = worker
        return await call_ray_actor_with_timeout(
            actor.is_pool_broken.remote(slot),
            timeout=self.config.loader.marker_timeout,
            task_description=f"MarkerWorker slot {slot} health check",
        )

    async def _reset_worker_pool(self, worker):
        actor, slot = worker
        return await call_ray_actor_with_timeout(
            actor.setup_mp.remote(slot),
            timeout=self.config.loader.marker_timeout,
            task_description=f"MarkerWorker slot {slot} reset",
        )

    async def ensure_worker_pool_healthy(self, worker):
        if await self._check_pool_broken(worker):
            self.logger.warning(f"Worker ProcessPoolExecutor for slot {worker[1]} is broken. Reinitializing it...")
            await self._reset_worker_pool(worker)

    async def _run_chunk(self, worker, file_path: str, page_range: list[int] | None, label: str):
        actor, slot = worker
        return await call_ray_actor_with_timeout(
            actor.process_pdf.remote(file_path, page_range=page_range, slot=slot),
            timeout=self.config.loader.marker_timeout,
            task_description=f"MarkerPool PDF {label} ({file_path})",
        )

    async def _recycle_and_release(self, worker, label: str):
        """Rebuild a slot's executor before handing the slot back.

        Runs after a timed-out or cancelled ``_run_chunk``: ``run_in_executor``
        isn't cancellable, so the child may still be parsing when we get here.
        Recycling first (kills the slot's child, rebuilds its executor) stops the
        next dispatched chunk from landing on a slot still busy with this one
        (#723). Other slots on the same actor are untouched.

        A cancel delivered to the reset call itself (the same delete that is
        tearing down this chunk can recursively cancel it) is not a failed
        reset, so it is retried instead of dropping the slot. If recycling
        genuinely keeps failing, the worker is never returned to ``_queue`` —
        a slot silently lost is safer than one that might still be busy.
        """
        for cancel_attempt in range(_RECYCLE_CANCEL_RETRIES + 1):
            try:
                await retry_with_backoff(
                    lambda _i: self._reset_worker_pool(worker),
                    max_retries=self.config.loader.marker_max_task_retry,
                    base_delay=self.config.loader.marker_retry_base_delay,
                    task_description=f"MarkerWorker recycle after {label}",
                )
            except (asyncio.CancelledError, TaskCancelledError):
                if cancel_attempt >= _RECYCLE_CANCEL_RETRIES:
                    self.logger.exception(
                        f"MarkerWorker recycle after {label} kept getting cancelled; dropping its slot"
                    )
                    return
                self.logger.warning(f"MarkerWorker recycle after {label} was cancelled; retrying")
                await asyncio.sleep(_RECYCLE_CANCEL_RETRY_DELAY)
                continue
            except Exception:
                self.logger.exception(f"MarkerWorker recycle after {label} failed; dropping its slot")
                return
            break

        await self._queue.put(worker)
        self.logger.debug(f"MarkerWorker returned to pool for {label}")

    async def _process_chunk(self, file_path: str, page_range: list[int] | None, label: str):
        """Acquire a worker slot, process a PDF chunk, and release the slot.

        A fresh worker is acquired per attempt so a flaky worker can be
        sidestepped and ``ensure_worker_pool_healthy`` re-runs each time.
        Retries are handled by ``retry_with_backoff``.
        """

        async def attempt(_i: int):
            worker = await self._queue.get()
            completed = False
            child_may_still_run = False
            try:
                self.logger.info(f"MarkerWorker allocated for {label}")
                await self.ensure_worker_pool_healthy(worker)
                result = await self._run_chunk(worker, file_path, page_range, label)
                completed = True
                return result
            except (TimeoutError, asyncio.CancelledError, TaskCancelledError):
                child_may_still_run = True
                raise
            except Exception as exc:
                # The parse ceiling (#997) is the one failure that leaves the child
                # *alive*: it raises inside the child rather than killing it, and
                # freeing the objects afterwards need not bring VmData back below
                # the limit — one long-lived allocation near the top of the heap
                # keeps it from shrinking. Returning this slot to the pool would
                # hand the next, innocent chunk a child that fails for a reason
                # that is not its own, so recycle it the way a timeout does.
                #
                # Matched through the cause chain, not with `except MemoryError`:
                # the child's error reaches here as RayTaskError(MemoryError),
                # which `call_ray_actor_with_timeout` re-raises as RuntimeError
                # with the Ray error only as __cause__. A bare isinstance check
                # never fires in production, however well it passes in a test
                # that raises MemoryError directly.
                if caused_by(exc, MemoryError):
                    child_may_still_run = True
                raise
            finally:
                if completed:
                    await self._queue.put(worker)
                    self.logger.debug(f"MarkerWorker returned to pool for {label}")
                elif child_may_still_run:
                    self.logger.warning(f"MarkerWorker for {label} did not complete cleanly; recycling before reuse")
                    asyncio.create_task(self._recycle_and_release(worker, label))
                else:
                    # An ordinary exception (a parse error, or an OOM the kernel
                    # resolved by killing the child) means the child already
                    # stopped on its own; nothing to reclaim, and a recycle would
                    # only cost a respawn of the slot's child. The parse ceiling's
                    # MemoryError is handled above precisely because it is the one
                    # case where the child survives.
                    await self._queue.put(worker)
                    self.logger.debug(f"MarkerWorker returned to pool for {label} without recycling")

        try:
            return await retry_with_backoff(
                attempt,
                max_retries=self.config.loader.marker_max_task_retry,
                base_delay=self.config.loader.marker_retry_base_delay,
                task_description=f"MarkerPool PDF {label} ({file_path})",
                # A chunk over the ceiling is over it again every time, so retrying
                # costs ~4x the parse plus backoff and ends with the same error.
                no_retry=(MemoryError,),
            )
        except Exception as exc:
            # Raise the type itself: this exception crosses one more actor
            # boundary to `MarkerLoader`, and Ray carries the type across but
            # not `__cause__`, so a RuntimeError caused by a MemoryError arrives
            # there as a plain RuntimeError.
            if not caused_by(exc, MemoryError):
                raise
            raise MemoryError(f"Marker parse of {label} ran out of memory") from exc

    async def process_pdf(self, file_path: str):
        chunk_size = self.config.loader.marker_chunk_size

        total_pages = self._get_page_count(file_path)
        if _MAX_PDF_PAGES > 0 and total_pages > _MAX_PDF_PAGES:
            self.logger.warning(
                f"PDF has {total_pages} pages; processing only the first {_MAX_PDF_PAGES} (max page cap)"
            )
        chunks = marker_runtime.plan_chunks(total_pages, chunk_size)

        if len(chunks) == 1:
            page_range, label = chunks[0]
            return await self._process_chunk(file_path, page_range=page_range, label=label)

        page_count = min(total_pages, _MAX_PDF_PAGES) if _MAX_PDF_PAGES > 0 else total_pages
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

        return marker_runtime.join_chunks(results)


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


class RayMarkerConversion:
    """``MarkerConversion`` through the named ``MarkerPool`` Ray actor."""

    def __init__(self) -> None:
        self.config = load_config()

    def _pool(self) -> MarkerPool:
        """Look up the named ``MarkerPool`` actor; call once per dispatch.

        Never cached: this conversion lives as long as its indexer worker, and
        ``POST /actors/MarkerPool/restart`` replaces the pool with a new actor,
        so a cached handle would keep dispatching to the killed one. Store the
        result before calling a method on it: Ray's ``ActorMethod`` holds its
        handle weakly, so ``self._pool().process_pdf.remote()`` raises
        "Lost reference to actor".
        """
        # Lazily create the pool if bootstrap didn't (it only pre-warms the
        # globally-configured PDF backend; a preset can select marker even when
        # the global default is docling — see #569/#575).
        from services.workers.bootstrap import get_or_create_actor

        return get_or_create_actor("MarkerPool", MarkerPool, lifetime="detached")

    async def convert(self, file_path: str) -> tuple[str, dict]:
        pool = self._pool()
        try:
            return await call_ray_actor_with_timeout(
                pool.process_pdf.remote(file_path),
                timeout=self.config.loader.marker_timeout,
                task_description=f"MarkerLoader PDF loading ({file_path})",
            )
        except Exception as exc:
            # The pool raises MemoryError itself, but it reaches here wrapped in
            # the Ray error: raise the type the conversion promises.
            if not caused_by(exc, MemoryError):
                raise
            raise MemoryError(str(exc)) from exc


class MarkerLoader(BasePooledParser):
    """Public ``BasePooledParser`` facade over a Marker conversion.

    Hands each ``parse()`` call to its ``MarkerConversion`` (the Ray pool:
    ``RayMarkerConversion``). Marker requires a file path on disk, so
    ``Document.raw_bytes`` is materialized to a temporary file (via
    ``Document.as_temporary_file``) before handoff.

    Output: one ``TextBlock`` per page (1-indexed ``page_number``) plus
    one ``ImageBlock`` per Marker image. Each ``ImageBlock`` carries the
    ``![](key)`` markdown ref in ``metadata['markdown_ref']`` so a
    downstream caption stage can substitute the wrapped caption back
    into the markdown by string match. Captioning is not done here —
    see :class:`ImageBlock` for the parser→caption contract.
    """

    PAGE_SEP = PAGE_SEP
    # The page is read with int(), which refuses more than 4,300 digits, and the
    # PDF's own text can contain ``{N}[PAGE_SEP]``: a number no page count reaches
    # leaves it plain text, as ``PAGE_RE`` does for ``[PAGE_N]``.
    _PAGE_MARKER_RE = re.compile(r"\{(\d{1,9})\}" + re.escape(PAGE_SEP))

    def __init__(self, conversion: MarkerConversion) -> None:
        self.config = load_config()
        self._conversion = conversion

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        report_parser_substage("parse.convert", "Converting PDF with Marker")
        async with document.as_temporary_file() as path:
            markdown, images = await self._dispatch(str(path))

        # The figures are handed over unencoded (see ``_build_image_blocks``);
        # splitting the markdown of a long PDF still runs off the loop, which
        # also renews the leases of every other file on this worker.
        pages, image_blocks = await asyncio.to_thread(self._postprocess, markdown, images)
        text_blocks = [TextBlock(text=text, page_number=page) for page, text in pages]

        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=image_blocks,
            metadata=dict(document.metadata),
            page_count=pages[-1][0] if pages else 0,
        )

    # ----- helpers -----

    def _postprocess(self, markdown: str, images: dict) -> tuple[list[tuple[int, str]], list[ImageBlock]]:
        return self._split_pages(markdown), self._build_image_blocks(images)

    async def _convert_pdf(self, file_path: str):
        try:
            return await self._conversion.convert(file_path)
        except MemoryError as exc:
            # The conversion's message names the remote cause, but only as
            # "MemoryError" at the end of a chain of task names. Name the memory
            # failure up front, and the setting that decides it, so an admin
            # can act from the jobs view.
            raise MemoryError(self._out_of_memory_message()) from exc

    def _out_of_memory_message(self) -> str:
        limit_mb = self.config.loader.marker_parse_memory_limit_mb
        if limit_mb > 0:
            return (
                "Marker ran out of memory parsing this PDF: the parse exceeded "
                f"MARKER_PARSE_MEMORY_LIMIT_MB={limit_mb} MiB. Raise that limit to index this file."
            )
        return "Marker ran out of memory parsing this PDF."

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

        Each image is kept decoded in ``pil_image``: the caption stage
        PNG-encodes it only when it captions the document, so a partition
        without captioning never pays for it.
        """
        blocks: list[ImageBlock] = []
        for key, pil_image in images.items():
            blocks.append(
                ImageBlock(
                    pil_image=pil_image,
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

        matches = list(cls._PAGE_MARKER_RE.finditer(markdown))
        has_trailing_page = bool(markdown[matches[-1].end() :].strip()) if matches else bool(markdown.strip())
        total_pages = len(matches) + int(has_trailing_page and bool(matches))
        if not matches and markdown.strip():
            total_pages = 1
        if total_pages:
            report_parser_substage("parse.text", "Extracting page text", progress=0, total=total_pages)

        pairs: list[tuple[int, str]] = []
        cursor = 0
        last_page = 0
        for index, match in enumerate(matches, start=1):
            page = int(match.group(1))
            text = markdown[cursor : match.start()].strip()
            pairs.append((page, text))
            cursor = match.end()
            last_page = page
            report_parser_substage("parse.text", "Extracting page text", progress=index, total=total_pages)
        tail = markdown[cursor:].strip()
        if tail:
            pairs.append((last_page + 1, tail))
            report_parser_substage("parse.text", "Extracting page text", progress=len(pairs), total=total_pages)
        elif not pairs and markdown.strip():
            pairs.append((1, markdown.strip()))
        return pairs


__all__ = [
    "MarkerLoader",
    "MarkerPool",
    "MarkerWorker",
    "RayMarkerConversion",
    # Moved to ``marker_runtime``; still importable from here.
    "_apply_parse_memory_limit",
    "_as_parse_memory_error",
    "_force_kill_executor",
]
