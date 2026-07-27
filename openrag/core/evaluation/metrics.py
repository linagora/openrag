"""Metric computation for an evaluation run.

Aggregates the per-file indexing timings the worker collected. Pure: the
worker measures, this decides what the measurements mean.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from core.models.evaluation import (
    FileIndexingSample,
    IndexingMetrics,
)

_BYTES_PER_MB = 1024 * 1024


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile.

    ``statistics.quantiles`` needs at least two points and interpolates;
    nearest-rank keeps a single-file run meaningful and always returns a
    duration that was actually observed.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    # ceil, not round: the rank is ceil(fraction * n) by definition, and round()
    # breaks ties to even, selecting the wrong element on an odd integer rank.
    rank = math.ceil(fraction * len(ordered))
    return float(ordered[min(max(rank, 1), len(ordered)) - 1])


def indexing_metrics(samples: Sequence[FileIndexingSample], wall_seconds: float) -> IndexingMetrics:
    """Aggregate per-file timings into throughput figures.

    ``wall_seconds`` is the measured end-to-end duration of the indexing
    phase, which is what throughput is derived from — summing per-file
    durations would overstate speed whenever files are indexed concurrently.
    """
    succeeded = [s for s in samples if not s.failed]
    durations = [s.duration_seconds for s in succeeded]
    # Two different questions: how big the corpus is (every file the run
    # attempted, matching ``files_total``) and how fast it moved (only what
    # actually landed). A failed file has a size but contributed no throughput.
    total_bytes = sum(s.size_bytes for s in samples)
    indexed_bytes = sum(s.size_bytes for s in succeeded)

    by_extension: dict[str, dict[str, float]] = {}
    for sample in succeeded:
        extension = (Path(sample.filename).suffix or "(none)").lower()
        bucket = by_extension.setdefault(extension, {"files": 0.0, "seconds": 0.0})
        bucket["files"] += 1
        bucket["seconds"] += sample.duration_seconds
    for bucket in by_extension.values():
        bucket["mean_seconds"] = round(bucket["seconds"] / bucket["files"], 3)

    return IndexingMetrics(
        files_total=len(samples),
        files_failed=sum(1 for s in samples if s.failed),
        bytes_total=total_bytes,
        wall_seconds=round(wall_seconds, 3),
        files_per_minute=round(len(succeeded) / wall_seconds * 60, 2) if wall_seconds > 0 else 0.0,
        megabytes_per_second=(round(indexed_bytes / _BYTES_PER_MB / wall_seconds, 3) if wall_seconds > 0 else 0.0),
        p50_seconds=round(_percentile(durations, 0.50), 3),
        p95_seconds=round(_percentile(durations, 0.95), 3),
        by_extension=by_extension,
        samples=list(samples),
    )


__all__ = ["indexing_metrics"]
