"""What a Marker conversion needs, without Ray.

The page count and chunking, the converter settings, the child process's
initializer and conversion function, the memory ceiling and the force-kill of a
wedged child. ``MarkerWorker`` and ``MarkerPool`` run them in Ray actors; an
engine running in its own process imports them from here without Ray.
"""

from __future__ import annotations

import gc
import os
from typing import Any

import pypdfium2
import torch
from core.utils.child_process import kill_process_group, tie_to_parent
from core.utils.logging import get_logger
from core.utils.process_limits import apply_parse_memory_limit
from marker.converters.pdf import PdfConverter

logger = get_logger()

#: Marker ends each page with ``{N}`` and this separator (``paginate_output``).
PAGE_SEP = "[PAGE_SEP]"

# Parser-bomb cap: never process more than this many pages from one PDF, so a
# crafted high-page-count file can't exhaust CPU/memory during ingestion.
# 0 or negative disables the cap.
_MAX_PDF_PAGES = 2000

#: Headroom the ceiling must leave above the child's baseline ``VmData`` before a
#: parse has room to work. Below this the limit is far more likely to be a
#: misconfiguration than a tight budget, so it is reported rather than left to
#: surface as a 100% Marker failure rate.
_MIN_PARSE_HEADROOM_MB = 256

#: Torch's CPU allocator reports a refused allocation as a ``RuntimeError`` with
#: this text, not as ``MemoryError`` — measured under ``RLIMIT_DATA`` with torch
#: 2.7.1. CUDA's out-of-memory error is also a ``RuntimeError`` subclass, but it is
#: a different failure (device memory, not this ceiling), so the match is on this
#: message and never on the type.
_TORCH_CPU_ALLOC_FAILURE = "DefaultCPUAllocator: can't allocate memory"


def converter_config(config: Any) -> dict[str, Any]:
    """The settings every Marker conversion of this deployment runs with."""
    return {
        "output_format": "markdown",
        "paginate_output": True,
        "page_separator": PAGE_SEP,
        # One worker: pdftext extracts in the slot child itself. With more,
        # it starts processes of its own, which do not die with the child
        # and each get a full memory ceiling (#1167). The slots already
        # parse chunks in parallel.
        "pdftext_workers": 1,
        # Surya passes disable= explicitly, which beats TQDM_DISABLE: honor
        # it here (non-empty = off, as tqdm reads it), and turn the bars
        # off with JSON logs even when TQDM_DISABLE was not set (#1297).
        "disable_tqdm": bool(os.environ.get("TQDM_DISABLE")) or config.verbose.format == "json",
    }


def page_count(file_path: str) -> int:
    pdf = pypdfium2.PdfDocument(file_path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def create_chunks(page_count: int, chunk_size: int) -> list[tuple[list[int], str]]:
    if page_count <= chunk_size:
        return [(list(range(page_count)), f"({page_count}p)")]
    chunks = []
    for start in range(0, page_count, chunk_size):
        end = min(start + chunk_size, page_count)
        page_range = list(range(start, end))
        label = f"[p{start}-{end - 1}]"
        chunks.append((page_range, label))
    return chunks


def plan_chunks(total_pages: int, chunk_size: int) -> list[tuple[list[int] | None, str]]:
    """The ``(page_range, label)`` chunks a PDF of *total_pages* pages is converted in.

    At most ``_MAX_PDF_PAGES`` pages. A ``None`` page range means every page;
    a capped PDF always gets explicit ranges, since ``None`` would convert the
    whole file and bypass the cap. ``chunk_size`` 0 or below converts the PDF in
    one piece.
    """
    capped = _MAX_PDF_PAGES > 0 and total_pages > _MAX_PDF_PAGES
    page_count = min(total_pages, _MAX_PDF_PAGES) if _MAX_PDF_PAGES > 0 else total_pages
    if chunk_size <= 0:
        # When capped, restrict to the first page_count pages instead of all.
        return [(list(range(page_count)), f"(first {page_count}p)") if capped else (None, "(all pages)")]
    chunks = create_chunks(page_count, chunk_size)
    if len(chunks) == 1:
        page_range, label = chunks[0]
        return [(page_range if capped else None, label)]
    return chunks


def join_chunks(results: list[tuple[str, dict]]) -> tuple[str, dict]:
    """The chunks' markdown in page order, and all their images."""
    all_markdown = []
    all_images = {}
    for markdown, images in results:
        all_markdown.append(markdown)
        all_images.update(images)
    return "\n\n".join(all_markdown), all_images


def _force_kill_executor(executor, log) -> None:
    """SIGKILL an executor's worker processes, then shut it down.

    ``ProcessPoolExecutor.shutdown()`` only asks workers to exit *after* their
    current task finishes. A worker wedged on a pathological PDF never finishes,
    so a plain shutdown leaves it running — holding its pool slot and the GPU
    indefinitely (#659). Killing the OS processes directly is the only way to
    reclaim a wedged worker. ``ProcessPoolExecutor`` doesn't expose which
    process ran a given task, which is why ``MarkerWorker`` gives every slot its
    own single-process executor: killing one never touches another slot's parse.

    Each worker leads its own process group (``tie_to_parent``), and the whole
    group is killed, so nothing a worker started outlives it (#1167).
    """
    if executor is None:
        return
    # Snapshot before shutdown(): the shutdown machinery clears ``_processes``.
    procs = list(getattr(executor, "_processes", {}).values())
    for proc in procs:
        try:
            # A broken pool keeps its crashed child, already reaped: its pid may
            # belong to another process now. Nothing it started is left to
            # reclaim, since pdftext runs inside the child (``pdftext_workers``).
            if proc.exitcode is not None:
                continue
            kill_process_group(proc.pid)
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


def _as_parse_memory_error(exc: BaseException) -> MemoryError | None:
    """The ``MemoryError`` a child failure stands for, or ``None``.

    Only the top-level exception *type* crosses the process pool back to the
    parent — ``concurrent.futures`` replaces the chain with a remote traceback —
    so the guards in ``_process_chunk`` see ``MemoryError`` only if the child
    raises exactly that. Two shapes would otherwise arrive as something else:
    torch's allocator ``RuntimeError``, and a ``MemoryError`` a library wrapped in
    its own exception. ``None`` when *exc* already is one, or is unrelated.
    """
    if isinstance(exc, MemoryError):
        return None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MemoryError) or (
            isinstance(current, RuntimeError) and _TORCH_CPU_ALLOC_FAILURE in str(current)
        ):
            return MemoryError(str(current))
        current = current.__cause__ or current.__context__
    return None


def _apply_parse_memory_limit(memory_limit_mb: int) -> None:
    """Cap what this child process may allocate, so one parse cannot take the pod.

    Runs in the slot's child process, which ``MarkerPool`` hands at most one chunk
    at a time — so with page chunking on each chunk runs under this bound rather
    than a whole document doing so. The limit is set once per child and persists
    across chunks until that child is recycled, so it is a per-*child* ceiling
    that each chunk shares, not a fresh allowance per chunk. A chunk that blows through it
    raises ``MemoryError`` in the process responsible instead of tripping a
    pod-level OOM kill that takes every file sharing the worker with it (#997).

    The limit covers the child's *whole* ``VmData`` — torch, Marker's startup
    allocations and the parse together — and Linux accepts a limit below what the
    process already uses, after which every allocation fails. The baseline is
    logged beside the ceiling and thin headroom is reported, because otherwise a
    plausible-looking value yields 100% Marker failures with nothing saying why.

    ``RLIMIT_DATA`` bounds the heap and private anonymous mappings. Deliberately
    not ``RLIMIT_AS``: that also counts file-backed mappings, so it would refuse
    the model weights and CUDA's device maps and break the parse outright.

    The ceiling is per process. Marker runs pdftext with one worker, which
    extracts text inside the child instead of starting processes, so one ceiling
    bounds a chunk's whole parse.

    Best-effort — a platform without ``RLIMIT_DATA``, or an existing limit
    below the request, must not stop the worker from starting.
    """
    apply_parse_memory_limit(
        memory_limit_mb,
        process="Marker child",
        setting="MARKER_PARSE_MEMORY_LIMIT_MB",
        min_headroom_mb=_MIN_PARSE_HEADROOM_MB,
    )


def init_child(model_dict, memory_limit_mb: int = 0, parent_pid: int | None = None):
    """Initializer of a slot's child process: the models, then the memory ceiling.

    Lives in the same module as ``convert_in_child``, which reads the models it
    stores: a child imports only the module of the function it runs.
    """
    global worker_model_dict
    # First, so a child whose actor is already gone exits before loading
    # anything, and one whose actor dies later dies with it (#1167).
    if parent_pid is not None:
        tie_to_parent(parent_pid)
    worker_model_dict = model_dict
    # Logged before the ceiling, like `_apply_parse_memory_limit`'s own lines:
    # under a tight limit the log call's allocation could raise MemoryError,
    # and nothing catches it in the pool initializer.
    logger.debug("Worker initialized with model dictionary")
    _apply_parse_memory_limit(memory_limit_mb)


def convert_in_child(file_path, config):
    """Convert *file_path* in a slot's child process, with the models ``init_child`` stored."""
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
        ceiling = _as_parse_memory_error(e)
        if ceiling is not None:
            raise ceiling from e
        raise
    finally:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()


__all__ = [
    "PAGE_SEP",
    "convert_in_child",
    "converter_config",
    "create_chunks",
    "init_child",
    "join_chunks",
    "page_count",
    "plan_chunks",
]
