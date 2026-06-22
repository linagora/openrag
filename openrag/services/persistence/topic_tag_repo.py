"""asyncpg-backed :class:`TopicTagRepository`."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.ports.topic_tag_repo import TopicTagRepository

if TYPE_CHECKING:
    import asyncpg


class PgTopicTagRepository(TopicTagRepository):
    """Store document-level topic tags in Postgres."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        result = {
            "document_id": row["document_id"],
            "partition": row["partition"],
            "tag": row["tag"],
        }
        try:
            result["created_at"] = row["created_at"]
        except KeyError:
            pass
        return result

    async def bulk_insert(self, tags: list[dict]) -> int:
        rows = _normalize_rows(tags)
        if not rows:
            return 0

        return await self.pool.fetchval(
            """
            WITH rows AS (
                SELECT *
                FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])
                  AS t(document_id, partition, tag, normalized_tag)
            ),
            inserted AS (
                INSERT INTO topic_tags (document_id, partition, tag, normalized_tag)
                SELECT document_id, partition, tag, normalized_tag
                FROM rows
                ON CONFLICT (document_id, partition, normalized_tag)
                DO UPDATE SET tag = EXCLUDED.tag
                RETURNING 1
            )
            SELECT COUNT(*)::int FROM inserted
            """,
            [row["document_id"] for row in rows],
            [row["partition"] for row in rows],
            [row["tag"] for row in rows],
            [row["normalized_tag"] for row in rows],
        )

    async def get_by_document(self, document_id: str, partition: str) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT document_id, partition, tag, created_at
            FROM topic_tags
            WHERE document_id = $1 AND partition = $2
            ORDER BY normalized_tag
            """,
            document_id,
            partition,
        )
        return [self._row_to_dict(row) for row in rows]

    async def delete_by_document(self, document_id: str, partition: str) -> int:
        result = await self.pool.execute(
            "DELETE FROM topic_tags WHERE document_id = $1 AND partition = $2",
            document_id,
            partition,
        )
        return _delete_count(result)

    async def search(self, partition: str, tag: str, top_k: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT document_id, partition, tag, created_at
            FROM topic_tags
            WHERE partition = $1 AND normalized_tag = $2
            ORDER BY document_id
            LIMIT $3
            """,
            partition,
            _normalize_key(tag),
            max(1, top_k),
        )
        return [self._row_to_dict(row) for row in rows]


def _normalize_rows(tags: list[dict]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for tag in tags:
        document_id = str(tag.get("document_id") or "").strip()
        partition = str(tag.get("partition") or "").strip()
        display_tag = _normalize_display_tag(tag.get("tag"))
        normalized_tag = _normalize_key(display_tag)
        if not document_id or not partition or not display_tag:
            continue
        key = (document_id, partition, normalized_tag)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "document_id": document_id,
                "partition": partition,
                "tag": display_tag,
                "normalized_tag": normalized_tag,
            }
        )
    return rows


def _normalize_display_tag(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:80]


def _normalize_key(value: str) -> str:
    return _normalize_display_tag(value).casefold()


def _delete_count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


__all__ = ["PgTopicTagRepository"]
