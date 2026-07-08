"""Shared stub primitives for repositories with no current source code.

Phase 7A.2 ships placeholders for ports that have no equivalent in the
legacy :class:`components.indexer.vectordb.utils.PartitionFileManager`.
These are deliberate scaffolds — the architecture is hexagonal so that
adding the real implementation later is a one-file change. Until then
every stub method raises :class:`StubRepositoryError`, a distinctive
exception subclass that grep-finds easily when the post-refactoring
features come online.

Why not silent fallbacks (return ``None`` / empty list)? Because that
hides bugs: an orchestrator that quietly retrieves zero rows from a
"feature does not exist" repo behaves indistinguishably from a real
empty repo. A loud exception forces the caller to opt in to the gap.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class StubRepositoryError(NotImplementedError):
    """Raised by every stub repository method.

    Subclass of :class:`NotImplementedError` so existing ``except
    NotImplementedError`` blocks still catch it, but distinguishable
    from a third-party library's NotImplementedError when tracing
    production issues.
    """


def stub_not_implemented(feature: str) -> StubRepositoryError:
    """Build a uniformly-phrased error so tracebacks are self-explanatory."""
    return StubRepositoryError(
        f"{feature} is on the post-refactoring roadmap — see REFACTORING/Phase 7A.2 stubs.",
    )


class _StubRepositoryBase:
    """Common asyncpg pool plumbing for stubs.

    A stub still receives a ``pool_getter`` callable so that when the
    real implementation lands the constructor signature does not need
    to change — only the method bodies.
    """

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()


__all__ = ["StubRepositoryError", "stub_not_implemented", "_StubRepositoryBase"]
