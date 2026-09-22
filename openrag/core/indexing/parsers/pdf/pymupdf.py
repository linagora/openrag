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

Two things follow, and both were measured (300-page PDF, 4 parses):
threads cannot parallelize this at all — one dedicated thread took 92s
and four threads took 113s, slower as well as unsafe — while four
processes took 31s. And a child process can be given a hard memory
ceiling, so a crafted PDF fails as one parse instead of OOM-killing the
worker and every file sharing it (#997).
"""

from __future__ import annotations

import asyncio
import resource
import threading
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing import get_context
from typing import Literal

import pymupdf
import pymupdf4llm
from core.utils.logging import get_logger

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


_POOL_LOCK = threading.Lock()
_POOL_SETTINGS = PyMuPDFPoolSettings()
_POOL: Executor | None = None


def configure_pool(settings: PyMuPDFPoolSettings) -> None:
    """Install pool settings, discarding any pool already built on the old ones."""
    global _POOL_SETTINGS, _POOL
    with _POOL_LOCK:
        if settings == _POOL_SETTINGS and _POOL is not None:
            return
        _POOL_SETTINGS = settings
        old, _POOL = _POOL, None
    if old is not None:
        old.shutdown(wait=False, cancel_futures=True)


def _pool_worker_init(memory_limit_mb: int) -> None:
    """Cap what one parse may allocate, in the child that runs it.

    ``RLIMIT_DATA``, not ``RLIMIT_AS``: measured, ``RLIMIT_AS`` also refuses
    file-backed mappings, which a PDF parse needs. Best-effort — a platform that
    refuses the call must still yield a usable worker.
    """
    if memory_limit_mb <= 0:
        return
    try:
        limit = memory_limit_mb * 1024 * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_DATA)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))
    except (ValueError, OSError, AttributeError) as exc:  # pragma: no cover - platform dependent
        logger.warning(f"Could not apply PyMuPDF parse memory limit ({memory_limit_mb} MiB): {exc}")


def _build_pool(settings: PyMuPDFPoolSettings) -> Executor:
    if settings.max_workers <= 1 and settings.memory_limit_mb <= 0:
        # Nothing asked for, so nothing changes: the one dedicated thread, as
        # before. Spawning a child process by default would alter behaviour
        # everywhere this parser is built — including **each API replica**,
        # which constructs the same dispatcher for the direct-extract path
        # (``di/container.py``: "this parser lives in each API replica"). There
        # a first parse would spawn on the request loop that also serves
        # ``/health_check``, and under ``uv run -m api.main`` (the Ray Serve
        # entrypoint) a spawned child re-imports ``api.main`` as
        # ``__mp_main__`` — the whole app, per parse worker.
        #
        # The pool is worth having where indexing happens; it is opt-in so that
        # is a deliberate act rather than a side effect of upgrading.
        return ThreadPoolExecutor(max_workers=1, thread_name_prefix="pymupdf")

    # "spawn", not fork: this runs inside a Ray actor with threads already
    # started, and forking those is how you get a child that deadlocks on an
    # allocator lock it inherited mid-hold.
    return ProcessPoolExecutor(
        max_workers=max(1, settings.max_workers),
        initializer=_pool_worker_init,
        initargs=(settings.memory_limit_mb,),
        mp_context=get_context("spawn"),
        max_tasks_per_child=settings.max_tasks_per_child or None,
    )


def _get_pool() -> Executor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _build_pool(_POOL_SETTINGS)
        return _POOL


def _discard_pool(broken: Executor) -> None:
    """Drop a pool a child died in, so the next parse builds a fresh one.

    A child killed outright — the kernel OOM killer, or a segfault in MuPDF —
    breaks the whole executor, and every later submit would raise
    ``BrokenProcessPool`` forever. Only discard the pool we actually failed on:
    a concurrent caller may already have replaced it.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is broken:
            _POOL = None
    broken.shutdown(wait=False, cancel_futures=True)


def _extract_text(raw: bytes, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return one stripped plain-text string per page; no images."""
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        return [page.get_text().strip() for page in doc], []


def _to_markdown(doc: pymupdf.Document) -> list[dict]:
    return pymupdf4llm.to_markdown(doc, page_chunks=True, embed_images=False, write_images=False)


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
            pages = [(chunk.get("text") or "").strip() for chunk in chunks]
            return pages, []
        except RuntimeError as exc:
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
    pages = [(chunk.get("text") or "").strip() for chunk in chunks]
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

        pool = _get_pool()
        try:
            pages, images = await asyncio.get_running_loop().run_in_executor(
                pool, self._extract, document.raw_bytes, document.filename
            )
        except BrokenProcessPool:
            # The child died rather than raising — OOM killer, or a segfault in
            # MuPDF. Without discarding it, every later parse in this worker
            # would fail too, long after the file that caused it is gone.
            _discard_pool(pool)
            raise
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
