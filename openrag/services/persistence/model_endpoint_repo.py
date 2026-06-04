"""Stub :class:`ModelEndpointRepository`.

Model endpoints (embedder URLs, LLM URLs, reranker URLs etc.) are
configured in Hydra YAML today — runtime can't add/swap them without a
restart. Phase 14 replaces this stub with a DB-backed registry so
operators can repoint endpoints from an admin UI without restart.
"""

from __future__ import annotations

from core.config.model_endpoints import ModelEndpointRow
from core.ports.model_endpoint_repo import ModelEndpointRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgModelEndpointRepository(_StubRepositoryBase, ModelEndpointRepository):
    """TODO: real impl once the ``model_endpoints`` table is added (Phase 14C)."""

    async def create(self, row: ModelEndpointRow) -> ModelEndpointRow:
        raise stub_not_implemented("DB-backed model endpoints")

    async def get(self, name: str, model_type: str) -> ModelEndpointRow | None:
        raise stub_not_implemented("DB-backed model endpoints")

    async def list_all(self, model_type: str | None = None) -> list[ModelEndpointRow]:
        raise stub_not_implemented("DB-backed model endpoints")

    async def update(self, name: str, model_type: str, **fields: object) -> ModelEndpointRow | None:
        raise stub_not_implemented("DB-backed model endpoints")

    async def rename(self, name: str, model_type: str, new_name: str) -> None:
        raise stub_not_implemented("DB-backed model endpoints")

    async def delete(self, name: str, model_type: str) -> bool:
        raise stub_not_implemented("DB-backed model endpoints")

    async def set_default(self, model_type: str, name: str) -> None:
        raise stub_not_implemented("DB-backed model endpoints")


__all__ = ["PgModelEndpointRepository"]
