"""PyMuPDF-backed PDF ``DocumentParser``.

The lightweight, no-VLM, no-GPU PDF backend. Uses ``pymupdf`` (a.k.a.
``fitz``) for plain-text extraction and ``pymupdf4llm`` for Markdown
extraction. Operates on ``Document.raw_bytes`` — file I/O is upstream.

Neither mode produces ``ImageBlock``s: ``embed_images=False`` and
``write_images=False`` keep base64 data out of the text (small chunks, no
rendering cost) and image-aware parsing is Marker's and Docling's job. The
docstring here previously described an ``embed_images=True`` path that the
code has never taken.

Concurrency note: PyMuPDF is **not** thread-safe — concurrent calls to
``page.get_text`` / ``pymupdf4llm.to_markdown`` from different threads
can raise ``ValueError: not a textpage of this page`` (upstream
maintainer position: documented limitation, won't fix). That is a
*thread* constraint: each process gets its own MuPDF state, so parses
are run in a pool of child processes rather than on one shared thread.

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
import gc
import pickle
import re
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing import get_context
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

#: MuPDF reports a refused allocation as an error whose message names the
#: allocator call — ``code=2: calloc (18904 x 40 bytes) failed`` — not as
#: ``MemoryError``. Measured under ``RLIMIT_DATA`` with PyMuPDF 1.26.
_MUPDF_ALLOC_FAILURE = re.compile(r"\b(?:malloc|calloc|realloc)\b.*\bfailed\b")

#: The ceiling this process runs under, set by the pool initializer. Read only
#: to name it in the error a parse over it raises; 0 where none applies.
_CHILD_MEMORY_LIMIT_MB = 0


def _pool_worker_init(memory_limit_mb: int) -> None:
    """Cap what one parse may allocate, in the child that runs it."""
    global _CHILD_MEMORY_LIMIT_MB
    _CHILD_MEMORY_LIMIT_MB = memory_limit_mb
    apply_parse_memory_limit(
        memory_limit_mb,
        process="PyMuPDF child",
        setting="PYMUPDF_PARSE_MEMORY_LIMIT_MB",
        min_headroom_mb=_MIN_PARSE_HEADROOM_MB,
    )


def _is_allocation_failure(exc: BaseException) -> bool:
    """Whether *exc*, or anything it was raised from, is a refused allocation."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MemoryError) or _MUPDF_ALLOC_FAILURE.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _out_of_memory_message(limit_mb: int) -> str:
    if limit_mb > 0:
        return (
            "PyMuPDF ran out of memory parsing this PDF: the parse exceeded "
            f"PYMUPDF_PARSE_MEMORY_LIMIT_MB={limit_mb} MiB. Raise that limit to index this file."
        )
    return "PyMuPDF ran out of memory parsing this PDF."


def _child_died_message(limit_mb: int) -> str:
    return (
        "PyMuPDF's child process died while parsing this PDF. With "
        f"PYMUPDF_PARSE_MEMORY_LIMIT_MB={limit_mb} MiB set, that limit is the likeliest cause: "
        "raise it to index this file."
    )


def _survives_pickling(exc: BaseException) -> bool:
    try:
        pickle.loads(pickle.dumps(exc))
    except Exception:  # noqa: BLE001 - any failure means it cannot cross back
        return False
    return True


def _run_extract(
    extract: Callable[[bytes, str], tuple[list[str], list[ImageBlock]]], raw: bytes, filename: str
) -> tuple[list[str], list[ImageBlock]]:
    """Run *extract* and turn its failure into one the parent can act on.

    Runs where the parse runs — in the child when the pool uses processes. Two
    failures would otherwise reach the parent as something misleading:

    - a refused allocation arrives as MuPDF's own error, or as a bare
      ``MemoryError`` with no message; it becomes a ``MemoryError`` naming the
      setting that decides it.
    - MuPDF's SWIG exceptions cannot be pickled, and the pool reports that as
      ``TypeError: cannot pickle 'SwigPyObject' object`` instead of the error
      itself; it becomes a ``RuntimeError`` carrying the original text.
    """
    try:
        return extract(raw, filename)
    except Exception as exc:
        try:
            out_of_memory = _is_allocation_failure(exc)
        except MemoryError:
            out_of_memory = True
        if not out_of_memory:
            if _survives_pickling(exc):
                raise
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    # Raised outside the handler, unchained, after freeing what can be freed:
    # until then the exception's traceback holds every frame of the failed parse
    # — the memory that hit the ceiling — and the pool formats that traceback to
    # send it back. MuPDF's resource store is C memory ``gc`` cannot see. Under a
    # tight ceiling building the message can still fail; the parent then names
    # the setting itself (``_ParsePool.run``).
    gc.collect()
    pymupdf.TOOLS.store_shrink(100)
    raise MemoryError(_out_of_memory_message(_CHILD_MEMORY_LIMIT_MB))


def _retire_executor(executor: Executor) -> None:
    """Stop *executor* now, killing a child still running a parse.

    ``shutdown()`` only asks a child to exit once its current parse finishes; a
    cancelled or timed-out parse may never finish, so its child is killed.
    ``_processes`` is private, as in Marker's ``_force_kill_executor``: no public
    API names the process that runs a given task.
    """
    for process in list(getattr(executor, "_processes", {}).values()):
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - one unkillable child must not block the rest
            logger.warning("Could not kill a PyMuPDF child process", exc_info=True)
    executor.shutdown(wait=False, cancel_futures=True)


class _ParsePool:
    """Runs parses on ``max_workers`` slots, one parse per slot at a time.

    Without a pool asked for — one worker, no ceiling — the single slot is the
    one dedicated thread used before #997, so nothing changes. Otherwise each
    slot is its own ``ProcessPoolExecutor`` with one child: replacing a slot's
    child cannot fail a parse running in another, which one shared executor
    with several children does (it breaks as a whole when any child dies).

    Free slots are an ``asyncio.Queue`` bound to the event loop that first uses
    the pool; a different loop gets a fresh queue (tests run one loop each).
    """

    def __init__(self, settings: PyMuPDFPoolSettings) -> None:
        self._settings = settings
        self._slots: list[Executor | None] = [None] * max(1, settings.max_workers)
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
        executor = self._slots[slot]
        if executor is None:
            executor = self._build_executor()
            self._slots[slot] = executor
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
            max_tasks_per_child=self._settings.max_tasks_per_child or None,
        )

    def _replace(self, slot: int, executor: Executor) -> None:
        if self._slots[slot] is executor:
            self._slots[slot] = None
        _retire_executor(executor)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        free = self._free_slots()
        slot = await free.get()
        try:
            executor = self._executor(slot)
            try:
                return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)
            except (BrokenProcessPool, MemoryError, asyncio.CancelledError) as exc:
                # A dead child poisons its executor. A child over the ceiling
                # survives, but freeing the parse's objects need not bring its
                # heap back under the limit, so the next file would fail for a
                # reason that is not its own (measured). A cancelled or timed-out
                # parse keeps running in its child. Each needs a fresh child.
                if self.uses_processes:
                    self._replace(slot, executor)
                limit_mb = self._settings.memory_limit_mb
                # Measured under a tight ceiling: the child can die receiving the
                # next file (the pool's own unpickling raises MemoryError outside
                # any parse code), or send back a MemoryError with no message.
                if isinstance(exc, BrokenProcessPool) and limit_mb > 0:
                    raise MemoryError(_child_died_message(limit_mb)) from exc
                if isinstance(exc, MemoryError) and not str(exc):
                    raise MemoryError(_out_of_memory_message(limit_mb)) from exc
                raise
        finally:
            free.put_nowait(slot)

    def shutdown(self) -> None:
        for executor in self._slots:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
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


def _extract_text(raw: bytes, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return one stripped plain-text string per page; no images."""
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
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


def _extract_markdown(raw: bytes, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return structured Markdown per page (no images).

    pymupdf is the lightweight, no-VLM backend. ``pymupdf4llm`` preserves
    document structure (headings, lists, tables) — which the markdown-aware
    chunker needs to cut on real boundaries instead of mid-sentence — while
    ``embed_images=False`` keeps base64 image data out of the text. That keeps
    chunks small (no Milvus gRPC overflow) and skips image rendering entirely
    (fast). Image-aware parsing is marker/docling's job, so no ``ImageBlock``s
    are produced here.
    """
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
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
    # than held alongside the second. ``raw`` itself belongs to the caller's
    # Document and stays live either way (#846).
    with pymupdf.open(stream=cleaned, filetype="pdf") as clean_doc:
        chunks = _to_markdown(clean_doc)
        pages = _pages_with_fallback(clean_doc, chunks, filename)
    return pages, []


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

        pages, images = await _get_pool().run(_run_extract, self._extract, document.raw_bytes, document.filename)
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
