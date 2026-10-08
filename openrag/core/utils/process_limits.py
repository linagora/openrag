"""A memory ceiling for a parser's child process (#997, audit A2).

Shared by every backend that parses in a child process (Marker, PyMuPDF), so
the rules that make a ceiling usable — measure the baseline, refuse a ceiling
at or below it, warn when the headroom is thin — exist once.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()


def child_vmdata_mb() -> int | None:
    """This process's current ``VmData`` in MiB, or ``None`` where unreadable.

    ``VmData`` is exactly what ``RLIMIT_DATA`` bounds, so it is the number an
    operator needs to pick a ceiling. Linux-only and best-effort: callers treat
    ``None`` as "could not measure", never as zero.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmData:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def apply_parse_memory_limit(memory_limit_mb: int, *, process: str, setting: str, min_headroom_mb: int) -> None:
    """Cap what this process may allocate, so one parse cannot take the pod.

    Call it in the child that runs the parse. A parse that blows through the
    ceiling raises ``MemoryError`` there instead of tripping a pod-level OOM kill
    that takes every file sharing the worker with it.

    The limit covers the child's *whole* ``VmData`` — its imports, models and the
    parse together — and Linux accepts a limit below what the process already
    uses, after which every allocation fails. So the baseline is logged beside
    the ceiling, a ceiling at or below it is refused, and headroom under
    *min_headroom_mb* is reported: otherwise a plausible-looking value yields
    100% parse failures with nothing saying why.

    ``RLIMIT_DATA`` bounds the heap and private anonymous mappings. Deliberately
    not ``RLIMIT_AS``: that also counts file-backed mappings, so it would refuse
    model weights, CUDA's device maps and memory-mapped files.

    *process* names the child in log lines ("Marker child"); *setting* is the
    environment variable an operator changes.

    Best-effort — a platform without ``RLIMIT_DATA``, or an existing hard limit
    below the request, must not stop the worker from starting.
    """
    if memory_limit_mb <= 0:
        return
    try:
        # Imported here rather than at module scope: ``resource`` is Unix-only,
        # and a missing module raises at import time, where the best-effort
        # contract below cannot catch it.
        import resource

        limit = memory_limit_mb * 1024 * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_DATA)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        effective_mb = limit // (1024 * 1024)
        baseline_mb = child_vmdata_mb()
        if baseline_mb is None:
            logger.info(f"{process} memory limit set to {effective_mb} MiB (baseline unreadable)")
        elif effective_mb <= baseline_mb:
            # Applying it would fail every parse on this child, and with the
            # recycle on MemoryError each parse would also respawn the child —
            # churning processes while doing nothing. Keep it working unbounded
            # and say so loudly instead.
            logger.error(
                f"{process} memory limit {effective_mb} MiB is at or below this child's baseline "
                f"of {baseline_mb} MiB, so it is NOT applied — this child runs without a ceiling. "
                f"Raise {setting} well above {baseline_mb}, or unset it."
            )
            return
        elif effective_mb - baseline_mb < min_headroom_mb:
            logger.warning(
                f"{process} memory limit {effective_mb} MiB leaves only {effective_mb - baseline_mb} MiB "
                f"above this child's baseline of {baseline_mb} MiB; parses are likely to fail. "
                f"Consider at least {baseline_mb + min_headroom_mb} MiB."
            )
        else:
            logger.info(f"{process} memory limit set to {effective_mb} MiB (baseline {baseline_mb} MiB)")
        # Applied last, deliberately. Reading /proc and formatting these lines
        # can each need an allocation, and a ceiling at or below current VmData
        # makes any allocation raise MemoryError — which this handler does not
        # catch and a pool initializer has no outer handler for, so the
        # initializer would die with the diagnostics that explain why.
        resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))
    except (ImportError, ValueError, OSError, AttributeError) as exc:
        logger.warning(f"Could not apply {process} memory limit ({memory_limit_mb} MiB): {exc}")
