from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_SUCCESS_STAGE = "stored"


@dataclass(frozen=True)
class RowFailure:
    """Stage and error message for a single failed row."""

    stage: str
    error: str


@dataclass(frozen=True)
class BatchIngestSummary:
    """Aggregated result of a batch ingestion run."""

    total: int
    succeeded: int
    failed: int
    stored_count: int
    failures: tuple[RowFailure, ...] = field(default_factory=tuple)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.total if self.total > 0 else 0.0


def aggregate_batch_results(
    rows: Sequence[MutableMapping[str, Any]],
) -> BatchIngestSummary:
    """Summarise processed rows from :func:`ingest_batch`.

    A row is counted as succeeded when ``row["stage"] == "stored"``.
    All other rows are counted as failed regardless of whether an exception
    was raised.
    """
    succeeded = 0
    stored_count = 0
    failures: list[RowFailure] = []

    for row in rows:
        stage = row.get("stage", "")
        if stage == _SUCCESS_STAGE:
            succeeded += 1
            try:
                stored_count += int(row.get("stored_count", 0))
            except (TypeError, ValueError):
                pass
        else:
            failures.append(
                RowFailure(
                    stage=str(stage),
                    error=str(row.get("error", "")),
                )
            )

    return BatchIngestSummary(
        total=len(rows),
        succeeded=succeeded,
        failed=len(failures),
        stored_count=stored_count,
        failures=tuple(failures),
    )


__all__ = ["BatchIngestSummary", "RowFailure", "aggregate_batch_results"]
