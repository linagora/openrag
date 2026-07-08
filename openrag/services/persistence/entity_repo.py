"""Stub :class:`EntityRepository`.

Entity extraction (canonical name + aliases per partition) is a
post-refactoring NER feature with no current implementation. The port
shape is pinned so that when an extraction pipeline is added, only this
file changes.
"""

from __future__ import annotations

from core.ports.entity_repo import EntityRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgEntityRepository(_StubRepositoryBase, EntityRepository):
    """TODO: real impl once the ``entities`` table is added."""

    async def upsert(
        self,
        partition: str,
        entity_type: str,
        canonical_name: str,
        aliases: list[str],
    ) -> str:
        raise stub_not_implemented("NER / entity storage")

    async def search(self, partition: str, query: str, top_k: int = 10) -> list[dict]:
        raise stub_not_implemented("NER / entity storage")

    async def get_by_document(self, document_id: str) -> list[dict]:
        raise stub_not_implemented("NER / entity storage")

    async def delete_by_document(self, document_id: str) -> int:
        raise stub_not_implemented("NER / entity storage")


__all__ = ["PgEntityRepository"]
