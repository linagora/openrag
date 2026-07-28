"""Tests for indexing aggregation."""

from __future__ import annotations

from core.evaluation.metrics import indexing_metrics
from core.models.evaluation import FileIndexingSample


def _sample(name: str, seconds: float, size: int = 1024, failed: bool = False):
    return FileIndexingSample(filename=name, size_bytes=size, duration_seconds=seconds, failed=failed)


# ── indexing ─────────────────────────────────────────────────────────


def test_throughput_uses_wall_clock_not_summed_durations():
    """Files may be indexed concurrently, so summing per-file durations would
    overstate throughput."""
    metrics = indexing_metrics([_sample("a.pdf", 4.0), _sample("b.pdf", 4.0)], wall_seconds=4.0)
    assert metrics.files_per_minute == 30.0


def test_failed_files_are_counted_but_excluded_from_throughput():
    """A failure is part of the corpus but contributed no throughput, so it
    counts toward the totals and toward neither rate."""
    metrics = indexing_metrics(
        [_sample("a.pdf", 2.0, size=1024), _sample("b.pdf", 0.0, size=4096, failed=True)],
        wall_seconds=2.0,
    )
    assert metrics.files_total == 2
    assert metrics.files_failed == 1
    assert metrics.files_per_minute == 30.0
    # "Corpus size" in the UI — it must not shrink because a file failed.
    assert metrics.bytes_total == 1024 + 4096
    assert metrics.megabytes_per_second == round(1024 / (1024 * 1024) / 2.0, 3)


def test_percentiles_on_a_single_file_return_that_file():
    metrics = indexing_metrics([_sample("a.pdf", 3.0)], wall_seconds=3.0)
    assert metrics.p50_seconds == 3.0
    assert metrics.p95_seconds == 3.0


def test_p50_of_an_even_sample_takes_the_lower_middle():
    """Nearest-rank p50 is ceil(n/2); rounding half-to-even would report the
    slower file for even n whose half is odd."""
    metrics = indexing_metrics([_sample("a.pdf", 1.0), _sample("b.pdf", 10.0)], wall_seconds=11.0)
    assert metrics.p50_seconds == 1.0
    assert metrics.p95_seconds == 10.0


def test_p50_ignores_files_that_failed_to_index():
    metrics = indexing_metrics(
        [_sample("a.pdf", 5.0), _sample("b.pdf", 0.0, failed=True)],
        wall_seconds=5.0,
    )
    assert metrics.p50_seconds == 5.0


def test_zero_wall_time_does_not_divide_by_zero():
    metrics = indexing_metrics([_sample("a.pdf", 0.0)], wall_seconds=0.0)
    assert metrics.files_per_minute == 0.0
    assert metrics.megabytes_per_second == 0.0


def test_breakdown_is_grouped_by_lowercased_extension():
    metrics = indexing_metrics(
        [_sample("a.PDF", 2.0), _sample("b.pdf", 4.0), _sample("c.txt", 1.0)],
        wall_seconds=7.0,
    )
    assert metrics.by_extension[".pdf"]["files"] == 2
    assert metrics.by_extension[".pdf"]["mean_seconds"] == 3.0
    assert metrics.by_extension[".txt"]["files"] == 1
