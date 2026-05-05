"""Reciprocal Rank Fusion — pure math, no domain coupling.

Combines multiple ranked lists into a single ranking by summing reciprocal
ranks across lists. Items present in more lists, or higher-ranked in any
list, sort to the top of the fused result.

Formula:
    score(item) = Σ_i 1 / (k + rank_i)

with ``rank_i`` the 1-based rank of the item in list ``i``. Smaller ``k``
amplifies the top of each list; ``k=60`` is the canonical default and
balances rank sensitivity across lists.

Identification of "the same item" is delegated to the caller via
``key_fn`` — typically returning the chunk id, document id, or URL.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from typing import TypeVar

T = TypeVar("T")


def rrf_reranking(
    ranked_lists: Sequence[Sequence[T]],
    key_fn: Callable[[T], Hashable] | None = None,
    k: int = 60,
) -> list[T]:
    """Fuse multiple ranked lists into one via Reciprocal Rank Fusion.

    Args:
        ranked_lists: Each inner sequence is a ranked list (best first).
        key_fn: Returns the identity key for an item; items sharing a key
                across lists have their RRF scores summed. Defaults to
                ``id(item)`` (object identity), which prevents fusion across
                lists for items lacking a logical id.
        k: RRF dampening constant. ``60`` is canonical.

    Returns:
        A single ranked list, best first. Empty input -> empty list.
        Single input list is returned as-is.

    Raises:
        ValueError: if ``k < 0`` (would produce a zero or negative
        denominator at rank 1 or below and crash with ZeroDivisionError).
    """
    if k < 0:
        raise ValueError(f"RRF k must be non-negative, got {k}")
    if not ranked_lists:
        return []
    if len(ranked_lists) == 1:
        return list(ranked_lists[0])

    if key_fn is None:
        key_fn = id  # type: ignore[assignment]

    fused: dict[Hashable, tuple[float, T]] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_fn(item)
            score, kept = fused.get(key, (0.0, item))
            fused[key] = (score + 1.0 / (rank + k), kept)

    return [item for _, item in sorted(fused.values(), key=lambda x: x[0], reverse=True)]
