"""Shared file-count accounting for catalog deletion transactions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


async def decrement_file_counts(
    conn: asyncpg.Connection,
    deleted_rows: Iterable[Mapping[str, Any]],
) -> int:
    """Decrement counters for rows deleted by the current transaction."""
    counts = Counter(row["created_by"] for row in deleted_rows if row["created_by"] is not None)
    for user_id in sorted(counts):
        await conn.execute(
            "UPDATE users SET file_count = GREATEST(file_count - $1, 0) WHERE id = $2",
            counts[user_id],
            user_id,
        )
    return len(counts)
