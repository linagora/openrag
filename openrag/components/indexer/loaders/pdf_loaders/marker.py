import asyncio
import gc
import re
import time
from pathlib import Path

import pypdfium2
import ray
import torch
from config import load_config
from langchain_core.documents.base import Document
from marker.converters.pdf import PdfConverter
from utils.logger import get_logger

from ..base import BaseLoader

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

    async def ensure_worker_pool_healthy(self, worker):
        from components.ray_utils import call_ray_actor_with_timeout

        timeout = self.config.loader.marker_timeout
        broken = await call_ray_actor_with_timeout(
            worker.is_pool_broken.remote(),
            timeout=timeout,
            task_description="MarkerWorker pool health check",
        )
        if broken:
            self.logger.warning("Worker ProcessPoolExecutor is broken. Reinitializing pool...")
            await call_ray_actor_with_timeout(
                worker.setup_mp.remote(),
                timeout=timeout,
                task_description="MarkerWorker pool reset",
            )

    async def _process_chunk(self, file_path: str, page_range: list[int] | None, label: str):
        """Acquire a worker slot, process a PDF chunk, and release the slot.

        Retries on failure with exponential backoff up to marker_max_task_retry times.
        A fresh worker is acquired per attempt so a flaky worker can be sidestepped
        and ensure_worker_pool_healthy re-runs each time.
        """
        from components.ray_utils import call_ray_actor_with_timeout, retry_with_backoff

        timeout = self.config.loader.marker_timeout

        async def attempt(i: int):
            worker = await self._queue.get()
            try:
                self.logger.info(f"MarkerWorker allocated for {label} (attempt {i + 1})")
                await self.ensure_worker_pool_healthy(worker)
                future = worker.process_pdf.remote(file_path, page_range=page_range)
                return await call_ray_actor_with_timeout(
                    future,
                    timeout=timeout,
                    task_description=f"MarkerPool PDF {label} ({file_path})",
                )
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


class MarkerLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.page_sep = "[PAGE_SEP]"
        self.worker = ray.get_actor("MarkerPool", namespace="openrag")

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        from components.ray_utils import call_ray_actor_with_timeout

        if metadata is None:
            metadata = {}

        file_path_str = str(file_path)
        start = time.time()

        try:
            timeout = self.config.loader.marker_timeout
            future = self.worker.process_pdf.remote(file_path_str)
            markdown, images = await call_ray_actor_with_timeout(
                future,
                timeout=timeout,
                task_description=f"MarkerLoader PDF loading ({file_path_str})",
            )

            if not markdown:
                raise RuntimeError(f"Conversion failed for {file_path_str}")

            if self.image_captioning:
                keys = list(images.keys())
                captions = await self.caption_images(list(images.values()))
                for key, caption in zip(keys, captions):
                    markdown = markdown.replace(f"![]({key})", caption)

            else:
                logger.debug("Image captioning disabled.")

            markdown = markdown.split(self.page_sep, 1)[1]
            markdown = re.sub(r"\{(\d+)\}" + re.escape(self.page_sep), r"[PAGE_\1]", markdown)
            markdown = markdown.replace("<br>", "").strip()

            doc = Document(page_content=markdown, metadata=metadata)

            if save_markdown:
                self.save_content(markdown, file_path_str)

            duration = time.time() - start
            logger.info(f"Processed {file_path_str} in {duration:.2f}s")
            return doc

        except Exception:
            logger.exception("Error in aload_document", path=file_path_str)
            raise
