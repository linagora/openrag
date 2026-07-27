"""Port for evaluation dataset and run persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.evaluation import EvalDataset, EvalRun, EvalRunStatus


class EvaluationRepository(ABC):
    """Storage contract for evaluation datasets and runs."""

    @abstractmethod
    async def create_dataset(self, dataset: EvalDataset) -> EvalDataset:
        """Persist a new dataset row."""

    @abstractmethod
    async def list_datasets(self) -> list[EvalDataset]:
        """All datasets, newest first."""

    @abstractmethod
    async def get_dataset(self, dataset_id: str) -> EvalDataset | None:
        """One dataset, or ``None`` when it does not exist."""

    @abstractmethod
    async def delete_dataset(self, dataset_id: str) -> bool:
        """Delete a dataset. Returns ``False`` when nothing was deleted."""

    @abstractmethod
    async def create_run(self, run: EvalRun) -> EvalRun:
        """Persist a queued run."""

    @abstractmethod
    async def list_runs(self, limit: int = 50) -> list[EvalRun]:
        """Recent runs, newest first."""

    @abstractmethod
    async def get_run(self, run_id: str) -> EvalRun | None:
        """One run with its metrics, or ``None``."""

    @abstractmethod
    async def active_run(self) -> EvalRun | None:
        """The run currently occupying the runner, if any."""

    @abstractmethod
    async def update_run_status(self, run_id: str, status: EvalRunStatus, *, error: str | None = None) -> None:
        """Move a run to a new status, stamping ``finished_at`` when terminal."""

    @abstractmethod
    async def save_run_results(self, run: EvalRun) -> None:
        """Write the metric payloads and terminal status of a finished run."""


__all__ = ["EvaluationRepository"]
