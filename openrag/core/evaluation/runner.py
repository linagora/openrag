"""Port for handing an evaluation run to the worker layer.

``EvaluationService`` owns the orchestration around a run — provisioning the
throwaway partition and the service-user token, enforcing one-run-at-a-time,
recording the queued row. The run *itself* (upload the corpus over HTTP, shell
out to promptfoo, fold the outputs into metrics) executes inside the
``EvalRunner`` Ray actor.

Defining the three operations the orchestrator needs on a dedicated port keeps
it Ray-free — the Phase 9 rule that all Ray code lives under
``services/workers/`` (``docs/refactoring/REFACTORING_STRATEGY_v1.md``). The
adapter in ``services/workers/eval_dispatcher.py`` binds this to the actor;
tests bind it to a fake.

No Ray types cross this boundary — only plain strings, mappings and bools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class EvaluationRunner(ABC):
    """Operations the evaluation orchestrator needs from the worker layer."""

    @abstractmethod
    async def is_busy(self) -> bool:
        """Whether a run currently occupies the runner.

        Doubles as the orchestrator's liveness probe, so an implementation
        must raise rather than hang when the worker cannot be reached.
        """
        ...

    @abstractmethod
    async def dispatch(
        self,
        *,
        run_id: str,
        partition: str,
        token: str,
        api_base_url: str,
        corpus_dir: str,
        cases: Sequence[Mapping[str, Any]],
    ) -> None:
        """Hand a queued run to the worker.

        Fire-and-forget: the worker owns the run from here and records its own
        terminal status, so this returns as soon as the work is accepted.
        """
        ...

    @abstractmethod
    async def cancel(self, run_id: str) -> bool:
        """Ask the worker to abandon a run.

        Returns ``False`` when no worker owns ``run_id`` — the orchestrator
        reaps the orphaned row itself in that case.
        """
        ...


__all__ = ["EvaluationRunner"]
