"""Docling conversions in this process, without Ray.

``LocalDoclingEngine`` is a ``DoclingConversion``: one converter, at most
``DOCLING_MAX_TASKS_PER_WORKER`` conversions at a time, each in its own thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import torch
from core.utils.logging import get_logger
from core.utils.retry import retry_with_backoff

from .docling_runtime import build_converter

if TYPE_CHECKING:
    from docling.datamodel.document import ConversionResult

logger = get_logger()


class LocalDoclingEngine:
    """``DoclingConversion`` in this process.

    A thread can't be stopped, so a conversion past ``DOCLING_TIMEOUT`` fails
    its file but keeps its slot until its thread ends: the number of
    conversions running never exceeds the slots, whatever timed out or was
    abandoned. A file's timeout starts once it has a slot, not while it waits
    for one. Failed conversions are retried as the Ray pool retries them.
    """

    def __init__(self, config: Any, converter_factory: Callable[[], Any] = build_converter) -> None:
        loader = config.loader
        self._slots_total = loader.docling_max_tasks_per_worker
        self._timeout = loader.docling_timeout
        self._max_retries = loader.docling_max_task_retry
        self._retry_base_delay = loader.docling_retry_base_delay
        self._converter_factory = converter_factory
        self._converter: Any = None
        self._threads = ThreadPoolExecutor(max_workers=self._slots_total, thread_name_prefix="docling")
        # Created on first use, in the loop that converts.
        self._slots: asyncio.Semaphore | None = None
        self._starting: asyncio.Lock | None = None

    async def start(self) -> None:
        """Build the converter, loading its models. Idempotent; the first conversion calls it otherwise."""
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if self._converter is not None:
                return
            logger.info(
                f"Docling converts in this process, {self._slots_total} at a time; "
                "DOCLING_POOL_SIZE and DOCLING_NUM_GPUS apply to the Ray pool only."
            )
            self._converter = await asyncio.get_running_loop().run_in_executor(self._threads, self._converter_factory)

    async def convert(self, file_path: str) -> ConversionResult:
        await self.start()
        return await retry_with_backoff(
            lambda _attempt: self._convert_once(file_path),
            max_retries=self._max_retries,
            base_delay=self._retry_base_delay,
            task_description=f"Docling PDF ({file_path})",
        )

    def close(self) -> None:
        """Stop taking conversions. A thread still converting finishes on its own."""
        self._threads.shutdown(wait=False, cancel_futures=True)

    async def _convert_once(self, file_path: str) -> ConversionResult:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self._slots_total)
        slots = self._slots
        await slots.acquire()
        try:
            conversion = asyncio.get_running_loop().run_in_executor(self._threads, self._convert_in_thread, file_path)
        except BaseException:
            slots.release()
            raise
        # The slot goes back when the thread ends, not when this call does.
        conversion.add_done_callback(lambda _done: slots.release())
        # asyncio.wait never cancels the conversion: a timeout or a cancelled
        # caller leaves the thread running, and its slot held, until it ends.
        try:
            done, _ = await asyncio.wait({conversion}, timeout=self._timeout)
        except asyncio.CancelledError:
            conversion.add_done_callback(_retrieve_outcome)
            raise
        if conversion not in done:
            conversion.add_done_callback(_retrieve_outcome)
            raise TimeoutError(f"Docling conversion of {file_path} ran past {self._timeout}s")
        return conversion.result()

    def _convert_in_thread(self, file_path: str) -> ConversionResult:
        # Grad mode is per thread, so it is set in the thread that converts.
        # Docling's models also turn it off themselves around their inference.
        with torch.no_grad():
            return self._converter.convert(str(file_path))


def _retrieve_outcome(conversion: asyncio.Future) -> None:
    """Read the result of a conversion nobody waits for, so its error isn't reported as never retrieved."""
    if not conversion.cancelled():
        conversion.exception()


__all__ = ["LocalDoclingEngine"]
