"""Ray-free Marker PDF engine.

Extracted from ``marker_workers.MarkerWorker`` so the identical model-loading +
spawn ``ProcessPoolExecutor`` + per-unit conversion is reused by BOTH:
  * the legacy Ray path — ``MarkerWorker`` delegates here (behaviour unchanged), and
  * the marker-serve HTTP worker (C1) — pairs this engine with the chunk helpers
    below to process a whole PDF without Ray.

No Ray imports — plain Python. Models are loaded once in ``__init__``; a
``ProcessPoolExecutor`` is used (not ``multiprocessing.Pool``) because pdftext
spawns child processes and daemon pool workers cannot.
"""

from __future__ import annotations

import asyncio
import gc
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import pypdfium2
import torch
from core.utils.logging import get_logger
from marker.converters.pdf import PdfConverter

logger = get_logger()

PAGE_SEP = "[PAGE_SEP]"

# Parser-bomb cap: never process more than this many pages from one PDF, so a
# crafted high-page-count file can't exhaust CPU/memory. 0 or negative disables it.
MAX_PDF_PAGES = 2000


def page_count(file_path: str) -> int:
    pdf = pypdfium2.PdfDocument(file_path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def create_chunks(pages: int, chunk_size: int) -> list[tuple[list[int], str]]:
    if pages <= chunk_size:
        return [(list(range(pages)), f"({pages}p)")]
    chunks = []
    for start in range(0, pages, chunk_size):
        end = min(start + chunk_size, pages)
        chunks.append((list(range(start, end)), f"[p{start}-{end - 1}]"))
    return chunks


def _worker_init(model_dict):
    global worker_model_dict
    worker_model_dict = model_dict
    logger.debug("Marker executor child initialized with model dictionary")


def _process_pdf(file_path, config):
    global worker_model_dict

    page_range = config.get("page_range")
    label = f"[p{page_range[0]}-{page_range[-1]}]" if page_range is not None else "(all pages)"
    try:
        logger.debug("Processing PDF", path=file_path, label=label)
        converter = PdfConverter(artifact_dict=worker_model_dict, config=config)
        return converter(file_path)
    except Exception as e:
        logger.exception("Error processing PDF", path=file_path, label=label, error=str(e))
        raise
    finally:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()


class MarkerEngine:
    """Loads Marker models once + a spawn ProcessPoolExecutor; converts one PDF
    (optionally a page range) per ``process_pdf`` call. Ray-free."""

    def __init__(self, config):
        self.config = config
        self._max_processes = config.loader.marker_max_processes
        self._timeout = config.loader.marker_timeout
        self.converter_config = {
            "output_format": "markdown",
            "paginate_output": True,
            "page_separator": PAGE_SEP,
            "pdftext_workers": config.loader.marker_pdftext_workers,
            "disable_multiprocessing": False,
        }
        self.executor: ProcessPoolExecutor | None = None
        self._load_models()
        self.reset()

    def _load_models(self):
        from marker.models import create_model_dict

        self.model_dict = create_model_dict()
        for v in self.model_dict.values():
            if hasattr(v.model, "share_memory"):
                v.model.share_memory()

    def reset(self):
        """(Re)create the ProcessPoolExecutor (also used to recover a broken pool)."""
        import torch.multiprocessing as mp

        if self.executor:
            logger.warning("Resetting Marker ProcessPoolExecutor")
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

        try:  # spawn is required for CUDA in the child processes
            if mp.get_start_method(allow_none=True) != "spawn":
                mp.set_start_method("spawn", force=True)
        except RuntimeError:
            logger.warning("Process start method already set, using existing method")

        self.executor = ProcessPoolExecutor(
            max_workers=self._max_processes,
            initializer=_worker_init,
            initargs=(self.model_dict,),
            mp_context=mp.get_context("spawn"),
            max_tasks_per_child=self.config.loader.marker_max_tasks_per_child,
        )
        logger.info(f"MarkerEngine ready ({self._max_processes} executor slots)")

    async def process_pdf(self, file_path: str, page_range: list[int] | None = None) -> tuple[str, dict]:
        converter_config = self.converter_config.copy()
        if page_range is not None:
            converter_config["page_range"] = page_range

        def run_with_timeout():
            future = self.executor.submit(_process_pdf, file_path, converter_config)
            try:
                return future.result(timeout=self._timeout)
            except FuturesTimeoutError:
                logger.exception("Marker child process timed out", path=file_path)
                raise
            except Exception:
                logger.exception("Error processing with MarkerEngine", path=file_path)
                raise

        result = await asyncio.get_event_loop().run_in_executor(None, run_with_timeout)
        return result.markdown, result.images

    def is_broken(self) -> bool:
        # ProcessPoolExecutor auto-replaces dead/finished workers on next submit();
        # only a None or shut-down executor requires reinitialization.
        return self.executor is None or bool(getattr(self.executor, "_broken", False))

    def close(self):
        executor = getattr(self, "executor", None)
        if executor:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass  # best-effort cleanup


__all__ = ["MarkerEngine", "PAGE_SEP", "MAX_PDF_PAGES", "page_count", "create_chunks"]
