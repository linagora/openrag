"""PyMuPDF-backed PDF ``DocumentParser``.

The lightweight, no-VLM, no-GPU PDF backend. Uses ``pymupdf`` (a.k.a.
``fitz``) for plain-text extraction and ``pymupdf4llm`` for Markdown
extraction. Operates on ``Document.raw_bytes``; a child process opens
``Document.source_path`` instead when there is one (see :func:`_parse_source`).

Neither mode produces ``ImageBlock``s: ``embed_images=False`` and
``write_images=False`` keep base64 data out of the text (small chunks, no
rendering cost) and image-aware parsing is Marker's and Docling's job. The
docstring here previously described an ``embed_images=True`` path that the
code has never taken.

Concurrency note: PyMuPDF is **not** thread-safe — concurrent calls to
``page.get_text`` / ``pymupdf4llm.to_markdown`` from different threads
can raise ``ValueError: not a textpage of this page`` (upstream
maintainer position: documented limitation, won't fix). That is a
*thread* constraint: each process gets its own MuPDF state, so once
configured (``PYMUPDF_POOL_SIZE`` above 1, or a memory limit) parses run in
a pool of child processes. Unconfigured, they keep the one dedicated thread.

Two things follow, and both were measured (300-page PDF, 4 concurrent
parses): threads cannot parallelize this at all — one dedicated thread took
90.5s and four threads took 113s, slower as well as unsafe — while four
processes took 25.4s. And a child process can be given a hard memory
ceiling, so a crafted PDF fails as one parse instead of OOM-killing the
worker and every file sharing it (#997).

Each pool slot is its own single-child executor, as Marker's are: a child
that hits the ceiling, dies, or is abandoned by a cancelled parse is replaced
without touching a parse running in another slot.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import pickle
import re
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.process import BaseProcess
from typing import Any, Literal

import pymupdf
import pymupdf4llm
from core.utils.logging import get_logger
from core.utils.process_limits import apply_parse_memory_limit

from ....models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ..document_parser import DocumentParser
from ..registry import parser_registry

ParseMode = Literal["markdown", "text"]

logger = get_logger()


@dataclass(frozen=True)
class PyMuPDFPoolSettings:
    """How the parse pool is sized and bounded.

    Defaults keep the pre-#997 behaviour — one parse at a time, no ceiling — so
    a deployment that configures nothing sees no change. ``services`` pushes the
    real values in via :func:`configure_pool`; ``core`` never reads config.
    """

    max_workers: int = 1
    memory_limit_mb: int = 0
    max_tasks_per_child: int = 20


#: Headroom the ceiling should leave above a child's baseline ``VmData``. A
#: 300-page text PDF was measured to peak about 40 MiB above it; below this the
#: limit is more likely a misconfiguration than a tight budget, so it is reported.
_MIN_PARSE_HEADROOM_MB = 128

#: MuPDF reports a refused allocation as an error, not as ``MemoryError``:
#: ``code=2: calloc (18904 x 40 bytes) failed`` from its allocator, or
#: ``FT_New_Memory_Face(): out of memory`` from FreeType. Measured under
#: ``RLIMIT_DATA`` with PyMuPDF 1.26.
_MUPDF_ALLOC_FAILURE = re.compile(r"\b(?:malloc|calloc|realloc)\b.*\bfailed\b|\bout of memory\b")

#: The errors MuPDF raises for a JPEG 2000 image it could not read. Running out
#: of memory is one cause, a damaged image another: the text is the same.
_JPX_FAILURE = re.compile(r"\bFailed to (?:decode JPX image|read JPX header)\b")

#: What OpenJPEG logs to MuPDF's warnings when it is refused memory. Measured
#: under ``RLIMIT_DATA`` on JPEG 2000 images that parse without a ceiling: the
#: first three, on most failures but not all (a tiled image failed without any
#: at 600 and 900 MiB). The allocation phrases are OpenJPEG's other messages for
#: a refused allocation, from the bundled library's strings. None was logged for
#: the three damaged images tried, from 70 MiB to 1.4 GiB. A damaged image whose
#: header declares more than the ceiling does log one: OpenJPEG is refused the
#: memory before decoding reaches the damage, so that error says so too
#: (``_JPX_OUT_OF_MEMORY_HINT``).
_JPX_ALLOC_WARNING = re.compile(
    r"\bexceeds system limits\b|\bNot enough memory\b|\bCannot decode tile, memory error\b"
    r"|\b[Cc]annot allocate\b|\bError allocating\b|\ballocation fail(?:ure|ed)\b"
)

#: The ceiling this process runs under, set by the pool initializer. Read to
#: name it in the error a parse over it raises; 0 where none applies.
_CHILD_MEMORY_LIMIT_MB = 0

#: The ceiling the pool asked this child for, which an inherited limit can lower.
_CHILD_REQUESTED_LIMIT_MB = 0

#: Set in a pool child only: the thread shares its process with the rest of
#: the worker, whose memory is not the parse's to hand back.
_IN_POOL_CHILD = False

#: glibc's ``malloc_trim``, or ``None`` where the C library has none (musl).
_MALLOC_TRIM: Callable[[int], int] | None = None


class JPEG2000ReadError(RuntimeError):
    """A JPEG 2000 image failed under a ceiling, with nothing logged about memory.

    The image may be damaged, or over the ceiling: in the second case the child
    may still hold what the image took (measured at 900 MiB, about 600 MiB of
    ``VmData`` after the parse's memory was released), so the next file would
    fail in it.
    The pool replaces the child either way.
    """


def _load_malloc_trim() -> Callable[[int], int] | None:
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def _pool_worker_init(memory_limit_mb: int) -> None:
    """Cap what one parse may allocate, in the child that runs it."""
    global _CHILD_MEMORY_LIMIT_MB, _CHILD_REQUESTED_LIMIT_MB, _IN_POOL_CHILD, _MALLOC_TRIM
    # Before the ceiling: past it, an allocation here could fail the
    # initializer, which has no handler (see ``apply_parse_memory_limit``).
    _MALLOC_TRIM = _load_malloc_trim()
    _IN_POOL_CHILD = True
    _CHILD_REQUESTED_LIMIT_MB = memory_limit_mb
    # The limit actually applied, which is 0 when it was refused and lower
    # than asked when an existing limit clamped it, so the error names that one.
    _CHILD_MEMORY_LIMIT_MB = apply_parse_memory_limit(
        memory_limit_mb,
        process="PyMuPDF child",
        setting="PYMUPDF_PARSE_MEMORY_LIMIT_MB",
        min_headroom_mb=_MIN_PARSE_HEADROOM_MB,
    )


def _release_parse_memory() -> None:
    """Hand back what a parse leaves behind, in a pool child that survives it.

    An idle child otherwise keeps it until it is replaced, after
    ``PYMUPDF_MAX_TASKS_PER_CHILD`` parses. It empties MuPDF's resource store,
    then has ``malloc_trim`` return freed heap to the system. That brings RSS
    back near the child's baseline, but not always ``VmData``, which is what the
    ceiling counts. Measured after a 60-page PDF of large images: RSS 84 MiB
    against 349 without this, while ``VmData``, 333 without it, varied from run
    to run between 224 and 282; after a large JPEG 2000 image, ``VmData`` was back
    near the baseline. What that buys the next file depends
    on what came before, measured at 900 MiB with a JPEG 2000 file that parses
    alone at 800: after one PDF of large images it failed without this and parsed
    with it; after another it failed either way.

    Best-effort: housekeeping must never fail, or replace, the parse it follows.
    """
    if not _IN_POOL_CHILD:
        return
    try:
        pymupdf.TOOLS.store_shrink(100)
        if _MALLOC_TRIM is not None:
            _MALLOC_TRIM(0)
    except Exception:  # noqa: BLE001 - see the docstring
        pass


def _is_allocation_failure(exc: BaseException) -> bool:
    """Whether *exc*, or anything it was raised from, is a refused allocation.

    Under a ceiling, two more errors count: a ``SystemError`` (measured, the
    binding can fail to build its own exception when memory runs out and
    raises ``error return without exception set`` instead), and a JPEG 2000
    failure that OpenJPEG logged as a refused allocation.
    """
    return _find_in_chain(exc, _is_refused_allocation)


def _is_refused_allocation(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError) or _MUPDF_ALLOC_FAILURE.search(str(exc)):
        return True
    return _CHILD_MEMORY_LIMIT_MB > 0 and (isinstance(exc, SystemError) or _jpx_ran_out_of_memory(exc))


def _is_jpx_failure(exc: BaseException) -> bool:
    return bool(_JPX_FAILURE.search(str(exc)))


def _jpx_ran_out_of_memory(exc: BaseException) -> bool:
    """Whether *exc* is a JPEG 2000 failure OpenJPEG logged as a refused allocation.

    Reads the warnings of the current parse only: ``_run_extract`` clears them
    before each parse in a child.
    """
    return _is_jpx_failure(exc) and bool(_JPX_ALLOC_WARNING.search(pymupdf.TOOLS.mupdf_warnings(reset=0)))


def _clear_mupdf_warnings() -> None:
    """Forget the warnings logged so far, so the next read holds only new ones.

    Flushed, not only reset: MuPDF counts a warning identical to the last one
    instead of logging it, so after a reset alone a parse repeating the previous
    parse's last warning would log only "... repeated 2 times...".
    """
    pymupdf.TOOLS.mupdf_warnings(reset=1)


def _find_in_chain(exc: BaseException, matches: Callable[[BaseException], bool]) -> bool:
    """Whether *exc*, or anything it was raised from, *matches*."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if matches(current):
            return True
        current = current.__cause__ or current.__context__
    return False


#: A ceiling the parent names may not be the one its child runs under: the
#: child refuses one at or below its baseline, and an inherited limit can lower
#: it. The child logs either when it starts; only the child knows which applies.
_IF_NOT_APPLIED = (
    "If the child logged at startup that a limit it inherited lowered that one, raise the inherited limit "
    "instead; if it logged that it did not apply it, PYMUPDF_PARSE_MEMORY_LIMIT_MB is not the cause."
)


def _out_of_memory_message(applied_mb: int, requested_mb: int) -> str:
    """The error for a parse over the ceiling *applied_mb* the child runs under."""
    if applied_mb <= 0:
        return "PyMuPDF ran out of memory parsing this PDF."
    if applied_mb < requested_mb:
        return (
            f"PyMuPDF ran out of memory parsing this PDF: the parse exceeded this child's {applied_mb} MiB "
            f"limit, which a limit it inherited lowered from PYMUPDF_PARSE_MEMORY_LIMIT_MB={requested_mb} MiB. "
            "Raise the inherited limit to index this file."
        )
    return (
        "PyMuPDF ran out of memory parsing this PDF: the parse exceeded "
        f"PYMUPDF_PARSE_MEMORY_LIMIT_MB={applied_mb} MiB. Raise that limit to index this file."
    )


#: Appended when a JPEG 2000 image ran out of memory: measured, a damaged image
#: declaring 60000 x 60000 pixels did so under every ceiling up to 16 GiB, and
#: failed as damaged without one.
_JPX_OUT_OF_MEMORY_HINT = (
    "It was a JPEG 2000 image: if the file still fails with a higher limit, or without one, the image is damaged."
)


def _jpx_failure_message(exc: BaseException, applied_mb: int, requested_mb: int) -> str:
    """The error for a JPEG 2000 failure under the ceiling *applied_mb*, with nothing logged about memory."""
    if applied_mb < requested_mb:
        limit = (
            f"this child's {applied_mb} MiB memory limit, which a limit it inherited lowered from "
            f"PYMUPDF_PARSE_MEMORY_LIMIT_MB={requested_mb} MiB,"
        )
        remedy = "raise the inherited limit"
    else:
        limit = f"this child's {applied_mb} MiB memory limit (PYMUPDF_PARSE_MEMORY_LIMIT_MB),"
        remedy = "raise it"
    return (
        f"{type(exc).__name__}: {exc}. A JPEG 2000 image in this PDF could not be read. Under {limit} "
        "the cause can be that limit, which OpenJPEG does not always report: if the file parses with a higher "
        f"limit, {remedy}; if it also fails without one, the image is damaged."
    )


def _parent_out_of_memory_message(limit_mb: int) -> str:
    """The error for an out-of-memory failure the child could not describe."""
    if limit_mb > 0:
        return (
            f"PyMuPDF ran out of memory parsing this PDF. PYMUPDF_PARSE_MEMORY_LIMIT_MB={limit_mb} MiB is set "
            f"and is the likeliest limit the parse exceeded: raise it to index this file. {_IF_NOT_APPLIED}"
        )
    return _out_of_memory_message(0, 0)


def _child_died_message(limit_mb: int) -> str:
    return (
        "PyMuPDF's child process died while parsing this PDF. With "
        f"PYMUPDF_PARSE_MEMORY_LIMIT_MB={limit_mb} MiB set, that limit is the likeliest cause: "
        f"raise it to index this file. {_IF_NOT_APPLIED}"
    )


def _child_killed_message(signal_number: int) -> str:
    try:
        name = signal.Signals(signal_number).name
    except ValueError:
        name = f"signal {signal_number}"
    return f"PyMuPDF's child process was killed by {name} while parsing this PDF."


def _survives_pickling(exc: BaseException) -> bool:
    try:
        pickle.loads(pickle.dumps(exc))
    except Exception:  # noqa: BLE001 - any failure means it cannot cross back
        return False
    return True


def _run_extract(
    extract: Callable[[bytes | str, str], tuple[list[str], list[ImageBlock]]], source: bytes | str, filename: str
) -> tuple[list[str], list[ImageBlock]]:
    """Run *extract* and turn its failure into one the parent can act on.

    Runs where the parse runs — in the child when the pool uses processes. Three
    failures would otherwise reach the parent as something misleading:

    - a refused allocation arrives as MuPDF's own error, or as a bare
      ``MemoryError`` with no message; it becomes a ``MemoryError`` naming the
      setting that decides it.
    - a JPEG 2000 image that fails under a ceiling, with nothing logged about
      memory, may be over the ceiling or damaged; it becomes a
      ``JPEG2000ReadError`` that says both, and the pool replaces the child.
    - MuPDF's SWIG exceptions cannot be pickled, and the pool reports that as
      ``TypeError: cannot pickle 'SwigPyObject' object`` instead of the error
      itself; it becomes a ``RuntimeError`` carrying the original text.

    A child the pool keeps — the parse succeeded, or failed for another reason
    than these three — hands back the memory the parse left
    (``_release_parse_memory``). The pool replaces the others.
    """
    if _IN_POOL_CHILD:
        # This parse's warnings only, which is how ``_jpx_ran_out_of_memory``
        # tells a refused allocation from a damaged image; they also never pile
        # up over a child's lifetime.
        _clear_mupdf_warnings()
    try:
        result = extract(source, filename)
    except Exception as exc:
        try:
            out_of_memory = _is_allocation_failure(exc)
            jpx = _find_in_chain(exc, _is_jpx_failure)
        except MemoryError:
            out_of_memory = True
            jpx = False
        if not out_of_memory:
            if _CHILD_MEMORY_LIMIT_MB > 0 and jpx:
                raise JPEG2000ReadError(
                    _jpx_failure_message(exc, _CHILD_MEMORY_LIMIT_MB, _CHILD_REQUESTED_LIMIT_MB)
                ) from exc
            _release_parse_memory()
            if _survives_pickling(exc):
                raise
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    else:
        _release_parse_memory()
        return result
    # Raised outside the handler, unchained, after freeing what can be freed:
    # until then the exception's traceback holds every frame of the failed parse
    # — the memory that hit the ceiling — and the pool formats that traceback to
    # send it back. MuPDF's resource store is C memory ``gc`` cannot see. Under a
    # tight ceiling building the message can still fail; the parent then names
    # the setting itself (``_ParsePool.run``).
    gc.collect()
    pymupdf.TOOLS.store_shrink(100)
    message = _out_of_memory_message(_CHILD_MEMORY_LIMIT_MB, _CHILD_REQUESTED_LIMIT_MB)
    if jpx:
        message = f"{message} {_JPX_OUT_OF_MEMORY_HINT}"
    raise MemoryError(message)


def _retire_executor(executor: Executor) -> None:
    """Stop *executor* now, killing a child still running a parse.

    ``shutdown()`` only asks a child to exit once its current parse finishes; a
    cancelled or timed-out parse may never finish, so its child is killed.
    ``_processes`` is private, as in Marker's ``_force_kill_executor``: no public
    API names the process that runs a given task. ``shutdown()`` sets it to
    ``None``, so a parse still awaiting an executor that was already retired
    finds nothing left to kill here, rather than an ``AttributeError``.
    """
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - one unkillable child must not block the rest
            logger.warning("Could not kill a PyMuPDF child process", exc_info=True)
    executor.shutdown(wait=False, cancel_futures=True)


def _child_process(executor: Executor) -> BaseProcess | None:
    """*executor*'s child, read before the executor is retired, which clears it.

    When a child dies, the pool's thread joins it but keeps it in ``_processes``
    (private, see :func:`_retire_executor`).
    """
    processes = list((getattr(executor, "_processes", None) or {}).values())
    return processes[0] if processes else None


#: How long a broken pool's dead child may take to report its exit code.
_EXIT_CODE_WAIT_SECONDS = 1.0


async def _dead_child_exit_code(process: BaseProcess | None) -> int | None:
    """The exit code of a child the pool reported dead, or ``None`` if unknown.

    Negative for a signal, as ``multiprocessing`` reports it. The pool fails the
    parse before its thread has joined the child, so the code can still be
    unknown when the parse ends: the child may not be reapable yet, or the
    pool's thread may be reaping it. Waits for it a little, without blocking the
    event loop.
    """
    if process is None:
        return None
    deadline = time.monotonic() + _EXIT_CODE_WAIT_SECONDS
    while process.exitcode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return process.exitcode


class _ParsePool:
    """Runs parses on ``max_workers`` slots, one parse per slot at a time.

    Without a pool asked for — one worker, no ceiling — the single slot is the
    one dedicated thread used before #997, so parses still run one at a time
    in-process; only ``_run_extract``'s error handling is new there. Otherwise each
    slot is its own ``ProcessPoolExecutor`` with one child: replacing a slot's
    child cannot fail a parse running in another, which one shared executor
    with several children does (it breaks as a whole when any child dies).

    Free slots are an ``asyncio.Queue`` bound to the event loop that first uses
    the pool; a different loop gets a fresh queue (tests run one loop each).
    """

    def __init__(self, settings: PyMuPDFPoolSettings) -> None:
        self._settings = settings
        self._slots: list[Executor | None] = [None] * max(1, settings.max_workers)
        self._parses = [0] * len(self._slots)
        self._closed = False
        self._free: asyncio.Queue[int] | None = None
        self._free_loop: asyncio.AbstractEventLoop | None = None

    @property
    def uses_processes(self) -> bool:
        return self._settings.max_workers > 1 or self._settings.memory_limit_mb > 0

    def _free_slots(self) -> asyncio.Queue[int]:
        loop = asyncio.get_running_loop()
        if self._free is None or self._free_loop is not loop:
            self._free = asyncio.Queue()
            for slot in range(len(self._slots)):
                self._free.put_nowait(slot)
            self._free_loop = loop
        return self._free

    def _executor(self, slot: int) -> Executor:
        if self._closed:
            # A parse that waited for a slot while ``configure_pool`` replaced
            # this pool: a child built now would belong to no pool, and nothing
            # would ever stop it.
            raise RuntimeError("The PyMuPDF parse pool was replaced while this parse waited for it; retry the file.")
        executor = self._slots[slot]
        if executor is None:
            executor = self._build_executor()
            self._slots[slot] = executor
            self._parses[slot] = 0
        return executor

    def _build_executor(self) -> Executor:
        if not self.uses_processes:
            # Nothing asked for, so nothing changes: the one dedicated thread,
            # as before. Spawning a child by default would also put a first
            # parse on whatever loop builds this parser — and the pool is only
            # configured where indexing happens, not in the API replicas, which
            # build the same parser for the direct-extract path.
            return ThreadPoolExecutor(max_workers=1, thread_name_prefix="pymupdf")
        # "spawn", not fork: this runs inside a Ray actor with threads already
        # started, and forking those is how you get a child that deadlocks on an
        # allocator lock it inherited mid-hold.
        return ProcessPoolExecutor(
            max_workers=1,
            initializer=_pool_worker_init,
            initargs=(self._settings.memory_limit_mb,),
            mp_context=get_context("spawn"),
        )

    def _replace(self, slot: int, executor: Executor) -> None:
        if self._slots[slot] is executor:
            self._slots[slot] = None
        _retire_executor(executor)

    def _count_parse(self, slot: int, executor: Executor) -> None:
        """Replace a slot's child once it has run ``max_tasks_per_child`` parses.

        Done here rather than by ``ProcessPoolExecutor(max_tasks_per_child=...)``:
        that spawns the next child on the executor's own thread, and when the
        spawn fails the thread dies without marking the executor broken, so the
        slot's next parse waits for the parse timeout (measured). Built here, the
        next child starts in ``submit()``, which raises the failure instead.
        """
        limit = self._settings.max_tasks_per_child
        if not self.uses_processes or limit <= 0 or self._slots[slot] is not executor:
            return
        self._parses[slot] += 1
        if self._parses[slot] >= limit:
            self._replace(slot, executor)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        free = self._free_slots()
        slot = await free.get()
        try:
            executor = self._executor(slot)
            try:
                parse = asyncio.get_running_loop().run_in_executor(executor, fn, *args)
            except Exception:
                # ``submit()`` queues the parse before it starts the child, so a
                # child that cannot start (no process slots or memory left) leaves
                # this parse queued for whichever child starts next, ahead of that
                # child's own file. A fresh executor drops it.
                if self.uses_processes:
                    self._replace(slot, executor)
                raise
            try:
                return await parse
            except (BrokenProcessPool, MemoryError, JPEG2000ReadError, asyncio.CancelledError) as exc:
                child = _child_process(executor)
                # A dead child poisons its executor. A child over the ceiling
                # survives, but freeing the parse's objects need not bring its
                # heap back under the limit, so the next file would fail for a
                # reason that is not its own (measured), and a JPEG 2000 image
                # that failed may have been over it. A cancelled or timed-out
                # parse keeps running in its child. Each needs a fresh child.
                if self.uses_processes:
                    self._replace(slot, executor)
                limit_mb = self._settings.memory_limit_mb
                if isinstance(exc, BrokenProcessPool):
                    if self._closed:
                        # ``configure_pool`` killed it, and retiring the executor
                        # cleared the exit code that would name the signal.
                        raise BrokenProcessPool(
                            "The PyMuPDF parse pool was replaced while this parse ran; retry the file."
                        ) from exc
                    exit_code = await _dead_child_exit_code(child)
                    # The ceiling can only end a child through a Python
                    # MemoryError (exit code 1). A signal is something else: a
                    # segfault on a malformed PDF, or the kernel's OOM killer,
                    # which means the ceiling is too high, not too low.
                    if exit_code is not None and exit_code < 0:
                        raise BrokenProcessPool(_child_killed_message(-exit_code)) from exc
                    # Measured under a tight ceiling: the child can die receiving
                    # the next file (the pool's own unpickling raises MemoryError
                    # outside any parse code). An exit code still unknown names
                    # no cause.
                    if limit_mb > 0 and exit_code is not None and exit_code > 0:
                        raise MemoryError(_child_died_message(limit_mb)) from exc
                # Also measured: a MemoryError sent back with no message.
                if isinstance(exc, MemoryError) and not str(exc):
                    raise MemoryError(_parent_out_of_memory_message(limit_mb)) from exc
                raise
            finally:
                self._count_parse(slot, executor)
        finally:
            free.put_nowait(slot)

    def shutdown(self) -> None:
        # Killed, not asked to exit: a running parse would otherwise outlive the
        # pool, and once ``shutdown()`` has cleared the executor's processes
        # nothing could kill it later, not even its own cancellation.
        self._closed = True
        for executor in self._slots:
            if executor is not None:
                _retire_executor(executor)
        self._slots = [None] * len(self._slots)


_POOL_LOCK = threading.Lock()
_POOL_SETTINGS = PyMuPDFPoolSettings()
_POOL: _ParsePool | None = None


def configure_pool(settings: PyMuPDFPoolSettings) -> None:
    """Install pool settings, discarding any pool already built on the old ones."""
    global _POOL_SETTINGS, _POOL
    with _POOL_LOCK:
        if settings == _POOL_SETTINGS and _POOL is not None:
            return
        _POOL_SETTINGS = settings
        old, _POOL = _POOL, None
    if old is not None:
        old.shutdown()


def _get_pool() -> _ParsePool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _ParsePool(_POOL_SETTINGS)
        return _POOL


def _open_pdf(source: bytes | str) -> pymupdf.Document:
    """Open *source*: the PDF's bytes, or the path of a file holding them."""
    if isinstance(source, str):
        return pymupdf.open(source, filetype="pdf")
    return pymupdf.open(stream=source, filetype="pdf")


def _extract_text(source: bytes | str, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return one stripped plain-text string per page; no images."""
    with _open_pdf(source) as doc:
        return [page.get_text().strip() for page in doc], []


def _to_markdown(doc: pymupdf.Document) -> list[dict]:
    return pymupdf4llm.to_markdown(doc, page_chunks=True, embed_images=False, write_images=False)


# Fraction of plain-text characters that pymupdf4llm must retain per page
# before we fall back to ``page.get_text()``. Pages whose markdown drops below
# this threshold are silently replaced by their plain-text equivalent.
# 0.2 means "keep at least 20 % of the text layer characters" (#1102).
_MARKDOWN_TEXT_RATIO_THRESHOLD = 0.2


def _pages_with_fallback(doc: pymupdf.Document, chunks: list[dict], filename: str) -> list[str]:
    """Return one text string per page, falling back per-page to ``page.get_text()``.

    ``pymupdf4llm`` silently drops text on some InDesign/CS-generated PDFs that
    have a valid text layer (issue #1102). For each page we compare the
    character count returned by the Markdown converter against the raw text
    layer. If the markdown retains less than ``_MARKDOWN_TEXT_RATIO_THRESHOLD``
    of the plain-text characters, we substitute ``page.get_text()`` for that
    page and log a warning so the silent data-loss is visible in logs.
    """
    pages: list[str] = []
    for i, (chunk, page) in enumerate(zip(chunks, doc)):
        md_text = (chunk.get("text") or "").strip()
        plain_text = page.get_text().strip()
        plain_len = len(plain_text)
        if plain_len > 0 and len(md_text) < plain_len * _MARKDOWN_TEXT_RATIO_THRESHOLD:
            logger.bind(
                filename=filename,
                page=i + 1,
                md_chars=len(md_text),
                plain_chars=plain_len,
            ).warning(
                "pymupdf4llm returned much less text than the text layer; falling back to page.get_text() for this page"
            )
            pages.append(plain_text)
        else:
            pages.append(md_text)
    return pages


def _extract_markdown(source: bytes | str, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return structured Markdown per page (no images).

    pymupdf is the lightweight, no-VLM backend. ``pymupdf4llm`` preserves
    document structure (headings, lists, tables) — which the markdown-aware
    chunker needs to cut on real boundaries instead of mid-sentence — while
    ``embed_images=False`` keeps base64 image data out of the text. That keeps
    chunks small (no Milvus gRPC overflow) and skips image rendering entirely
    (fast). Image-aware parsing is marker/docling's job, so no ``ImageBlock``s
    are produced here.
    """
    with _open_pdf(source) as doc:
        try:
            chunks = _to_markdown(doc)
            pages = _pages_with_fallback(doc, chunks, filename)
            return pages, []
        except RuntimeError as exc:
            # Out of memory is not a malformed document: a cleaned copy needs
            # the same memory again, so retrying only doubles the work.
            if _is_allocation_failure(exc):
                raise
            # MuPDF hard-errors on some legal-but-unusual object graphs — e.g.
            # Type3 fonts with no embedded font file trip "code=4: no font file
            # for digest" on a single page and take the whole document down
            # with them (openrag#640). `garbage=4, clean=True` rewrites the PDF,
            # dropping unreferenced/orphaned objects (including the bad font
            # refs) without touching visible content, and recovers the full
            # document — retry once against that cleaned copy before giving up.
            logger.bind(filename=filename, error=str(exc)).warning(
                "pymupdf4llm.to_markdown failed; retrying against a garbage-collected/cleaned copy"
            )
            cleaned = doc.tobytes(garbage=4, clean=True)
    # Outside the `with`: the failed document is closed before the cleaned copy
    # is opened, so MuPDF's parsed structures for the first are released rather
    # than held alongside the second. The source itself belongs to the caller's
    # Document and stays live either way (#846).
    with pymupdf.open(stream=cleaned, filetype="pdf") as clean_doc:
        chunks = _to_markdown(clean_doc)
        pages = _pages_with_fallback(clean_doc, chunks, filename)
    return pages, []


def _parse_source(document: Document, pool: _ParsePool) -> bytes | str:
    """What to hand the parse: the file's path where a child can open it.

    A child gets its arguments pickled through a pipe, so passing the bytes
    costs a transient copy in the parent per parse in flight and puts the
    whole file in the child, twice over, before parsing starts. The child
    shares this process's filesystem, so it opens the file itself. Measured on
    an 82 MiB PDF, the parent's extra peak went from +82 MiB to +1 in both
    modes, and the child's peak from 320 MiB to 238 in markdown mode (the
    default) and from 235 MiB to 72 in text mode. On the thread there is
    nothing to copy, and without a file there is nothing to open.
    """
    if pool.uses_processes and document.source_path and os.path.isfile(document.source_path):
        return document.source_path
    return document.raw_bytes


@parser_registry.register("pymupdf")
class PyMuPDFParser(DocumentParser):
    """Extract text from a PDF as one ``TextBlock`` per page (+ ImageBlocks in markdown mode).

    ``mode="markdown"`` (default) uses ``pymupdf4llm`` for layout-preserving
    Markdown — better for downstream embedding and chunking, and surfaces
    embedded images. ``mode="text"`` uses raw ``pymupdf`` for plain text —
    slightly faster, no formatting, no images.
    """

    def __init__(self, *, mode: ParseMode = "markdown") -> None:
        if mode not in ("markdown", "text"):
            raise ValueError(f"PyMuPDFParser: unsupported mode {mode!r}")
        self._mode = mode
        self._extract = _extract_text if mode == "text" else _extract_markdown

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        pool = _get_pool()
        pages, images = await pool.run(_run_extract, self._extract, _parse_source(document, pool), document.filename)
        # Keep one TextBlock per source page (including empties) so callers
        # can preserve a 1-to-1 mapping with the original PDF's pagination.
        text_blocks = [TextBlock(text=text, page_number=i) for i, text in enumerate(pages, start=1)]
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=len(pages),
        )
