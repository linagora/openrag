"""Storage adapters — concrete :class:`CatalogStore` and :class:`VectorStore`.

Phase 7 splits the legacy ``MilvusDB`` Ray god object into two clean
adapters that orchestrators consume through the core port ABCs:

* :class:`postgres_store.PostgresStore` — composes the asyncpg
  :class:`ConnectionManager` with every repository implementation under
  :mod:`services.persistence`, satisfying
  :class:`core.ports.catalog_store.CatalogStore`.
* :class:`milvus_store.MilvusVectorStore` — Milvus 2.6 backed vector ops
  satisfying :class:`core.vector_stores.VectorStore`.

The Ray actor that callers know today lives at
:mod:`services.storage.milvus_ray_shim` and will be folded into the
new stores during Phase 7C.
"""

from services.storage.milvus_store import MilvusVectorStore
from services.storage.postgres_store import PostgresStore

__all__ = ["MilvusVectorStore", "PostgresStore"]
