"""Ray adapter for :class:`~core.evaluation.runner.EvaluationRunner`.

Binds the port to the ``EvalRunner`` actor and keeps every Ray concern —
actor lookup, ``.remote()`` calls, timeout and cancellation handling — on this
side of the boundary, so ``EvaluationService`` never imports Ray.

The actor handle is resolved on first use rather than in ``__init__``:
``EvalRunner`` is a *detached* actor, so merely building this adapter must not
be what spawns it. Listing datasets should not start a worker process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.evaluation.runner import EvaluationRunner
from services.workers.ray_utils import call_ray_actor_with_timeout

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Bound on the calls that are awaited (liveness probe, cancellation).
#: ``dispatch`` is fire-and-forget and so has nothing to time out.
DEFAULT_TIMEOUT = 60.0


class RayEvaluationRunner(EvaluationRunner):
    """``EvaluationRunner`` backed by the ``EvalRunner`` Ray actor."""

    def __init__(self, namespace: str = "openrag", timeout: float = DEFAULT_TIMEOUT) -> None:
        self._namespace = namespace
        self._timeout = timeout
        self._actor: Any = None

    def _handle(self) -> Any:
        """Get-or-create the detached actor, memoised for the process."""
        if self._actor is None:
            from services.workers.eval_runner import build_eval_runner

            self._actor = build_eval_runner(namespace=self._namespace)
        return self._actor

    async def is_busy(self) -> bool:
        return await call_ray_actor_with_timeout(
            future=self._handle().is_busy.remote(),
            timeout=self._timeout,
            task_description="reaching the evaluation runner",
        )

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
        # Deliberately not awaited: the worker owns the run from here and
        # records its own outcome, so the ObjectRef is dropped.
        self._handle().run.remote(
            run_id=run_id,
            partition=partition,
            token=token,
            api_base_url=api_base_url,
            corpus_dir=corpus_dir,
            cases=[dict(case) for case in cases],
        )

    async def cancel(self, run_id: str) -> bool:
        return await call_ray_actor_with_timeout(
            future=self._handle().cancel.remote(run_id),
            timeout=self._timeout,
            task_description=f"cancelling evaluation run {run_id}",
        )


def from_ray_namespace(namespace: str = "openrag", timeout: float = DEFAULT_TIMEOUT) -> RayEvaluationRunner:
    """Build the adapter bound to the detached ``EvalRunner`` actor."""
    return RayEvaluationRunner(namespace=namespace, timeout=timeout)


__all__ = ["DEFAULT_TIMEOUT", "RayEvaluationRunner", "from_ray_namespace"]
