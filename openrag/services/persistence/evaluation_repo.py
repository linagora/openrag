"""asyncpg-backed :class:`EvaluationRepository`.

Backs the ``eval_datasets`` and ``eval_runs`` tables. Metric payloads round-trip
as JSONB; the dataclasses in ``core.models.evaluation`` define their shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from core.models.evaluation import (
    AnswerMetrics,
    EvalCaseResult,
    EvalDataset,
    EvalRun,
    EvalRunStatus,
    FileIndexingSample,
    IndexingMetrics,
    RetrievalMetrics,
)
from core.ports.evaluation_repo import EvaluationRepository
from core.utils.exceptions import ConflictError

if TYPE_CHECKING:
    import asyncpg


def _dump(payload: Any) -> str | None:
    """Serialise a metrics dataclass — or a list of them — for a JSONB column."""
    if payload is None:
        return None
    if isinstance(payload, list):
        return json.dumps([asdict(item) for item in payload])
    return json.dumps(asdict(payload) if hasattr(payload, "__dataclass_fields__") else payload)


def _load(raw: Any) -> Any:
    """asyncpg returns JSONB as ``str`` unless a codec is registered."""
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str | bytes) else raw


class PgEvaluationRepository(EvaluationRepository):
    """asyncpg-backed implementation of :class:`EvaluationRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── datasets ─────────────────────────────────────────────────────

    async def create_dataset(self, dataset: EvalDataset) -> EvalDataset:
        row = await self.pool.fetchrow(
            """
            INSERT INTO eval_datasets (id, name, corpus_file_count,
                                       testset_row_count, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            dataset.id,
            dataset.name,
            dataset.corpus_file_count,
            dataset.testset_row_count,
            dataset.created_by,
        )
        return self._row_to_dataset(row)

    async def list_datasets(self) -> list[EvalDataset]:
        rows = await self.pool.fetch("SELECT * FROM eval_datasets ORDER BY created_at DESC")
        return [self._row_to_dataset(row) for row in rows]

    async def get_dataset(self, dataset_id: str) -> EvalDataset | None:
        row = await self.pool.fetchrow("SELECT * FROM eval_datasets WHERE id = $1", dataset_id)
        return self._row_to_dataset(row) if row else None

    async def delete_dataset(self, dataset_id: str) -> bool:
        result = await self.pool.execute("DELETE FROM eval_datasets WHERE id = $1", dataset_id)
        return result.endswith(" 1")

    # ── runs ─────────────────────────────────────────────────────────

    async def create_run(self, run: EvalRun) -> EvalRun:
        import asyncpg

        try:
            row = await self.pool.fetchrow(
                """
                INSERT INTO eval_runs (id, dataset_id, status, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                run.id,
                run.dataset_id,
                run.status.value,
                run.created_by,
            )
        except asyncpg.UniqueViolationError as exc:
            # ux_eval_runs_single_active — another run is already in flight.
            raise ConflictError("An evaluation run is already in progress.") from exc
        return self._row_to_run(row)

    async def list_runs(self, limit: int = 50) -> list[EvalRun]:
        rows = await self.pool.fetch("SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT $1", limit)
        return [self._row_to_run(row) for row in rows]

    async def get_run(self, run_id: str) -> EvalRun | None:
        row = await self.pool.fetchrow("SELECT * FROM eval_runs WHERE id = $1", run_id)
        return self._row_to_run(row) if row else None

    async def update_run_status(self, run_id: str, status: EvalRunStatus, *, error: str | None = None) -> None:
        await self.pool.execute(
            """
            UPDATE eval_runs
               SET status = $2,
                   error = COALESCE($3, error),
                   finished_at = CASE WHEN $4 THEN now() ELSE finished_at END
             WHERE id = $1
            """,
            run_id,
            status.value,
            error,
            status.is_terminal,
        )

    async def save_run_results(self, run: EvalRun) -> None:
        await self.pool.execute(
            """
            UPDATE eval_runs
               SET status = $2,
                   indexing = $3::jsonb,
                   retrieval = $4::jsonb,
                   answer = $5::jsonb,
                   cases = $6::jsonb,
                   error = $7,
                   finished_at = now()
             WHERE id = $1
            """,
            run.id,
            run.status.value,
            _dump(run.indexing),
            _dump(run.retrieval),
            _dump(run.answer),
            _dump(run.cases),
            run.error,
        )

    # ── row mapping ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_dataset(row: Any) -> EvalDataset:
        return EvalDataset(
            id=row["id"],
            name=row["name"],
            corpus_file_count=row["corpus_file_count"],
            testset_row_count=row["testset_row_count"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    @staticmethod
    def _row_to_run(row: Any) -> EvalRun:
        indexing = _load(row["indexing"])
        retrieval = _load(row["retrieval"])
        answer = _load(row["answer"])
        cases = _load(row["cases"]) or []
        samples = indexing.pop("samples", []) if indexing else []
        return EvalRun(
            id=row["id"],
            dataset_id=row["dataset_id"],
            status=EvalRunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            indexing=(
                IndexingMetrics(
                    **indexing,
                    samples=[FileIndexingSample(**sample) for sample in samples],
                )
                if indexing
                else None
            ),
            retrieval=RetrievalMetrics(**retrieval) if retrieval else None,
            answer=AnswerMetrics(**answer) if answer else None,
            cases=[EvalCaseResult(**case) for case in cases],
            error=row["error"],
            created_by=row["created_by"],
        )


__all__ = ["PgEvaluationRepository"]
