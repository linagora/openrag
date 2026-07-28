"""Serialized execution for all in-process PyMuPDF work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

# PyMuPDF is not thread-safe. Parsing and structural evidence extraction must
# share this executor rather than each serializing only its own calls.
_PYMUPDF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pymupdf")


async def run_pymupdf[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one callable on the process-wide serialized PyMuPDF executor."""
    call = partial(function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_PYMUPDF_EXECUTOR, call)


__all__ = ["run_pymupdf"]
