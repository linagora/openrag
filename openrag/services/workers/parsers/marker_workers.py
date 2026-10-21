import asyncio
import gc
import os
import re
import threading
import time
from dataclasses import dataclass

import pypdfium2
import ray
import torch
from core.config import load_config
from core.indexing.image_preprocessor import pil_to_png_bytes
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import (
    Document,
    DocumentType,
    ImageBlock,
    ProcessedDocument,
    TextBlock,
)
from core.utils.child_process import kill_process_group, tie_to_parent
from core.utils.logging import get_logger
from core.utils.process_limits import apply_parse_memory_limit
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from ray.exceptions import TaskCancelledError
from surya.settings import settings as surya_settings

from ..ray_utils import call_ray_actor_with_timeout, caused_by, retry_with_backoff

logger = get_logger()


@dataclass(frozen=True)
class _MarkerMode:
    #: Converter keys this mode sets.
    converter: dict
    #: surya's shared helper servers it calls.
    helpers: tuple[str, ...]
    #: Whether it calls the OCR server (``SURYA_INFERENCE_URL``).
    needs_ocr_server: bool


# What each mode asks of Marker. Balanced with ``disable_ocr`` is not a
# combination: Marker then switches to the fast layout model.
_MARKER_MODES: dict[str, _MarkerMode] = {
    "balanced": _MarkerMode({"mode": "balanced"}, ("ocr_error",), True),
    "fast": _MarkerMode({"mode": "fast"}, ("fast_layout", "ocr_error"), True),
    "fast_no_ocr": _MarkerMode({"mode": "fast", "disable_ocr": True}, ("fast_layout",), False),
}


def _marker_mode(mode: str) -> _MarkerMode:
    try:
        return _MARKER_MODES[mode]
    except KeyError:
        raise ValueError(f"Unknown Marker mode {mode!r}; expected one of {', '.join(_MARKER_MODES)}") from None


def _cgroup_cpu_quota(root: str = "/sys/fs/cgroup") -> int | None:
    """The container's CPU limit in whole cores (rounded up), or ``None`` without one.

    cgroup v2 keeps it in ``cpu.max``, v1 in the ``cpu`` controller's CFS files.
    """
    for quota_file, period_file in (
        ("cpu.max", None),
        ("cpu/cpu.cfs_quota_us", "cpu/cpu.cfs_period_us"),
    ):
        try:
            with open(os.path.join(root, quota_file)) as f:
                fields = f.read().split()
            if period_file is not None:
                with open(os.path.join(root, period_file)) as f:
                    fields.append(f.read().strip())
            quota, period = fields[0], int(fields[1])
            if quota in ("max", "-1"):
                return None
            return max(1, -(-int(quota) // period))
        except (OSError, ValueError, IndexError):
            continue
    return None


def _helper_threads() -> int:
    """Threads for surya's shared helpers: every core this process may use.

    Bounded by the container's CPU limit: a pod still sees every core of its
    node, and torch threads past the quota only wait for it.
    """
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:  # not on Linux
        cores = os.cpu_count() or 1
    quota = _cgroup_cpu_quota()
    return min(cores, quota) if quota else cores


def _start_helpers(helpers: tuple[str, ...], threads: int) -> None:
    """Start, or attach to, surya's shared helper servers; runs in a throwaway child.

    surya starts a helper the first time a client calls it, as a detached process
    that inherits the caller's environment and limits, and every later client
    attaches to it through a sentinel file. Called from a slot's child, the
    helper would load torch under that child's memory ceiling and with the one
    thread Ray gives an actor (``OMP_NUM_THREADS=1``), for every slot sharing it.
    """
    os.environ["OMP_NUM_THREADS"] = str(threads)
    from PIL import Image
    from surya.fast_layout import FastLayoutPredictor
    from surya.ocr_error import OCRErrorPredictor

    if "fast_layout" in helpers:
        FastLayoutPredictor(use_order=False)([Image.new("RGB", (64, 64), "white")])
    if "ocr_error" in helpers:
        OCRErrorPredictor()(["warm-up"])


#: A slot child's Marker models, built by ``_worker_models``.
worker_model_dict: dict | None = None


def _worker_models() -> dict:
    """This child's Marker models: thin clients of the OCR server and surya's helpers.

    Built in the child, on its first parse: the clients hold locks, so they
    can't be handed over from the actor, and a failure here fails that parse
    rather than breaking the pool.
    """
    global worker_model_dict
    if worker_model_dict is None:
        worker_model_dict = create_model_dict()
    return worker_model_dict


def _force_kill_executor(executor, log) -> None:
    """SIGKILL an executor's worker processes, then shut it down.

    ``ProcessPoolExecutor.shutdown()`` only asks workers to exit *after* their
    current task finishes. A worker wedged on a pathological PDF never finishes,
    so a plain shutdown leaves it running — holding its pool slot indefinitely
    (#659). Killing the OS processes directly is the only way to reclaim a
    wedged worker. ``ProcessPoolExecutor`` doesn't expose which
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


def _marker_num_gpus(config) -> float:
    """Return Marker's Ray GPU reservation, falling back to CUDA detection.

    Ray scheduling must see GPU capacity before an actor can request a GPU
    fraction. If Ray cannot report cluster resources yet, use local CUDA
    availability as the fallback so single-node startup still honors the
    configured Marker GPU request.

    The workers hold no model since Marker 2.0, but the helper servers they
    start (fast layout, OCR-error) run on whatever GPU the worker sees, and Ray
    hides the GPUs from an actor that reserves none.
    """
    requested_gpus = config.loader.marker_num_gpus
    if requested_gpus <= 0:
        return 0
    try:
        return requested_gpus if ray.cluster_resources().get("GPU", 0) > 0 else 0
    except Exception as exc:
        logger.warning("Failed to query Ray cluster GPU resources; falling back to CUDA check", error=str(exc))
        return requested_gpus if torch.cuda.is_available() else 0


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
    not ``RLIMIT_AS``: that also counts file-backed mappings, such as the
    libraries torch loads, so a ceiling sized for the parse would break it
    outright. The child holds no model since Marker 2.0: the OCR VLM and surya's
    helpers run in servers of their own, outside the ceiling.

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


# Parser-bomb cap: never process more than this many pages from one PDF, so a
# crafted high-page-count file can't exhaust CPU/memory during ingestion.
# 0 or negative disables the cap.
_MAX_PDF_PAGES = 2000

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
        self.page_sep = "[PAGE_SEP]"

        self._workers = self.config.loader.marker_max_processes

        self.converter_config = {
            "output_format": "markdown",
            "paginate_output": True,
            "page_separator": self.page_sep,
            # One worker: pdftext extracts in the slot child itself. With more,
            # it starts processes of its own, which do not die with the child
            # and each get a full memory ceiling (#1167). The slots already
            # parse chunks in parallel.
            "pdftext_workers": 1,
        }
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
        # surya's helper servers this actor has started (see _ensure_helpers).
        self._helpers_started: set[str] = set()
        self._helpers_lock = threading.Lock()
        self.init_resources()

    def init_resources(self):
        # Before any child starts: the children inherit this environment, and
        # their surya clients read it once, at import.
        self._configure_surya_clients()
        for slot in range(self._workers):
            self.setup_mp(slot)
        self.logger.info(f"MarkerWorker initialized with {self._workers} single-process executors")

    def _configure_surya_clients(self) -> None:
        """Set how the children's surya clients reach the servers, and check the OCR one."""
        # The fast-layout helper sizes its thread pool from this, and without it
        # from OMP_NUM_THREADS, which Ray sets to 1 in this actor.
        os.environ.setdefault("FAST_LAYOUT_NUM_THREADS", str(_helper_threads()))

        url = surya_settings.SURYA_INFERENCE_URL
        if not url:
            # Unset, surya starts the OCR server itself with `docker run` at the
            # first OCR call, which cannot work inside our containers.
            os.environ["SURYA_INFERENCE_AUTOSTART"] = "false"
            self.logger.warning("SURYA_INFERENCE_URL is not set: of the Marker modes, only fast_no_ocr can run")
            return

        # Each slot's client otherwise sends as many requests as the whole
        # server takes, and past that its throughput falls.
        slots = self.config.loader.marker_pool_size * self._workers
        concurrency = self.config.loader.marker_ocr_concurrency
        if "SURYA_INFERENCE_PARALLEL" not in os.environ:
            parallel = max(1, concurrency // slots)
            os.environ["SURYA_INFERENCE_PARALLEL"] = str(parallel)
            if slots > concurrency:
                self.logger.warning(
                    f"Marker has {slots} slots (MARKER_POOL_SIZE x MARKER_MAX_PROCESSES) but its OCR server "
                    f"takes {concurrency} requests at once (MARKER_OCR_CONCURRENCY): with one request each, "
                    "the slots can queue more than it serves. Lower the slots or raise both the server's "
                    "capacity and MARKER_OCR_CONCURRENCY."
                )
        self.logger.info(
            f"Marker OCR server at {url}: {os.environ['SURYA_INFERENCE_PARALLEL']} requests in flight "
            f"per slot, {slots} slots"
        )
        try:
            from surya.inference import SuryaInferenceManager

            # Attaching checks /health and the model the server serves.
            SuryaInferenceManager().start()
        except Exception:
            # Not fatal: the server may still be loading. Parses that need OCR
            # fail with surya's own message until it answers.
            self.logger.exception(f"Marker OCR server at {url} is not usable yet")

    def _ensure_helpers(self, helpers: tuple[str, ...]) -> None:
        """Start the surya helper servers a mode calls, once per actor.

        Started here rather than by a slot's child (see ``_start_helpers``). A
        child attaches to them through surya's sentinel file, and starts one
        again itself only if it has died since.
        """
        if set(helpers) <= self._helpers_started:
            return
        from concurrent.futures import ProcessPoolExecutor

        import torch.multiprocessing as mp

        with self._helpers_lock:
            missing = tuple(h for h in helpers if h not in self._helpers_started)
            if not missing:
                return
            threads = int(os.environ["FAST_LAYOUT_NUM_THREADS"])
            self.logger.info(f"Starting Marker helper servers {', '.join(missing)} with {threads} threads")
            try:
                with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as starter:
                    starter.submit(_start_helpers, missing, threads).result()
            except Exception as exc:
                raise RuntimeError(f"Marker helper servers {', '.join(missing)} did not start: {exc}") from exc
            self._helpers_started.update(missing)

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
                # it would keep holding its slot (#659).
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
                initargs=(self.config.loader.marker_parse_memory_limit_mb, os.getpid()),
                mp_context=mp.get_context("spawn"),
                max_tasks_per_child=self.config.loader.marker_max_tasks_per_child,
            )
            self.logger.debug(f"MarkerWorker slot {slot} executor ready")

    @staticmethod
    def _worker_init(memory_limit_mb: int = 0, parent_pid: int | None = None):
        global worker_model_dict
        # First, so a child whose actor is already gone exits before loading
        # anything, and one whose actor dies later dies with it (#1167).
        if parent_pid is not None:
            tie_to_parent(parent_pid)
        worker_model_dict = None
        # Logged before the ceiling, like `_apply_parse_memory_limit`'s own lines:
        # under a tight limit the log call's allocation could raise MemoryError,
        # and nothing catches it in the pool initializer.
        logger.debug("Marker worker child initialized")
        _apply_parse_memory_limit(memory_limit_mb)

    @staticmethod
    def _process_pdf(file_path, config):
        page_range = config.get("page_range")
        if page_range is not None:
            label = f"[p{page_range[0]}-{page_range[-1]}]"
        else:
            label = "(all pages)"

        try:
            logger.debug("Processing PDF", path=file_path, label=label)
            converter = PdfConverter(
                artifact_dict=_worker_models(),
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
            # No CUDA cleanup: the child holds no model, and touching CUDA would
            # only give it a context of its own.
            gc.collect()

    async def process_pdf(self, file_path: str, page_range: list[int] | None = None, *, slot: int, mode: str):
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        marker_mode = _marker_mode(mode)
        converter_config = {**self.converter_config, **marker_mode.converter}
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
            self._ensure_helpers(marker_mode.helpers)
            with self._executor_locks[slot]:
                current_executor = self.executors[slot]
                future = current_executor.submit(self._process_pdf, file_path, converter_config)
            try:
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
                # The child is still parsing and won't stop on its own;
                # recycle this slot's executor to reclaim it so the slot
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
        (#659).
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

    async def _run_chunk(self, worker, file_path: str, page_range: list[int] | None, label: str, mode: str):
        actor, slot = worker
        return await call_ray_actor_with_timeout(
            actor.process_pdf.remote(file_path, page_range=page_range, slot=slot, mode=mode),
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

    async def _process_chunk(self, file_path: str, page_range: list[int] | None, label: str, mode: str):
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
                result = await self._run_chunk(worker, file_path, page_range, label, mode)
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

    async def process_pdf(self, file_path: str, mode: str | None = None):
        """Parse a PDF in Marker ``mode``; ``None`` is the global ``MARKER_MODE``."""
        mode = mode or self.config.loader.marker_mode
        if _marker_mode(mode).needs_ocr_server and not surya_settings.SURYA_INFERENCE_URL:
            # Refused up front: otherwise only the files with a page that needs
            # OCR would fail, and only once they reach it.
            raise RuntimeError(
                f"Marker mode {mode!r} needs the OCR server, and SURYA_INFERENCE_URL is not set. "
                "Deploy the OCR service, or use Marker mode fast_no_ocr."
            )
        chunk_size = self.config.loader.marker_chunk_size

        total_pages = self._get_page_count(file_path)
        capped = _MAX_PDF_PAGES > 0 and total_pages > _MAX_PDF_PAGES
        if capped:
            self.logger.warning(
                f"PDF has {total_pages} pages; processing only the first {_MAX_PDF_PAGES} (max page cap)"
            )
        page_count = min(total_pages, _MAX_PDF_PAGES) if _MAX_PDF_PAGES > 0 else total_pages

        if chunk_size <= 0:
            # When capped, restrict to the first page_count pages instead of all.
            page_range = list(range(page_count)) if capped else None
            label = f"(first {page_count}p)" if capped else "(all pages)"
            return await self._process_chunk(file_path, page_range=page_range, label=label, mode=mode)

        chunks = self._create_chunks(page_count, chunk_size)

        if len(chunks) == 1:
            page_range, label = chunks[0]
            # When capped, the single chunk only covers the first page_count pages,
            # so pass that explicit range — page_range=None would process the whole
            # file and bypass the cap. Uncapped, None means "all pages".
            return await self._process_chunk(
                file_path, page_range=(page_range if capped else None), label=label, mode=mode
            )

        self.logger.info(
            f"Splitting {page_count}-page PDF into {len(chunks)} chunks of ~{chunk_size} pages for parallel processing"
        )

        tasks = [
            asyncio.create_task(self._process_chunk(file_path, page_range, label, mode)) for page_range, label in chunks
        ]
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
        self.config = load_config()

    def _pool(self) -> MarkerPool:
        """Look up the named ``MarkerPool`` actor; call once per dispatch.

        Never cached: this loader lives as long as its indexer worker, and
        ``POST /actors/MarkerPool-v2/restart`` replaces the pool with a new actor,
        so a cached handle would keep dispatching to the killed one. Store the
        result before calling a method on it: Ray's ``ActorMethod`` holds its
        handle weakly, so ``self._pool().process_pdf.remote()`` raises
        "Lost reference to actor".
        """
        # Lazily create the pool if bootstrap didn't (it only pre-warms the
        # globally-configured PDF backend; a preset can select marker even when
        # the global default is docling — see #569/#575).
        from services.workers.bootstrap import MARKER_POOL_ACTOR_NAME, get_or_create_actor

        return get_or_create_actor(MARKER_POOL_ACTOR_NAME, MarkerPool, lifetime="detached")

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

    async def _convert_pdf(self, file_path: str):
        pool = self._pool()
        try:
            return await call_ray_actor_with_timeout(
                pool.process_pdf.remote(file_path),
                timeout=self.config.loader.marker_timeout,
                task_description=f"MarkerLoader PDF loading ({file_path})",
            )
        except Exception as exc:
            # The Ray wrapper's message names the remote cause, but only as
            # "MemoryError" at the end of a chain of task names. Name the memory
            # failure up front, and the setting that decides it, so an admin
            # can act from the jobs view.
            if not caused_by(exc, MemoryError):
                raise
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
        """
        blocks: list[ImageBlock] = []
        for key, pil_image in images.items():
            try:
                png_bytes = pil_to_png_bytes(pil_image)
            except Exception as exc:
                logger.warning(f"Failed to encode Marker image {key}: {exc}")
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
